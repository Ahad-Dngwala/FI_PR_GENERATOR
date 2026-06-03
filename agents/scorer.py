"""
FI-PR-GENERATOR — Issue Scorer Agent
Uses Groq (Llama 3.1 8B) to score GitHub issues.
Fast, free, reliable — dedicated to triage only.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

import structlog

from memory.schemas import IssueDecision, IssueScore, OrgMemory

log = structlog.get_logger(__name__)

# Scoring weights
WEIGHTS = {
    "clarity":              0.25,
    "scope":                0.20,
    "historical_similarity":0.20,
    "testability":          0.15,
    "activity":             0.10,
    "label_bonus":          0.10,
}

DECISION_THRESHOLDS = {
    IssueDecision.PROCEED:       75,
    IssueDecision.MANUAL_REVIEW: 60,
}

PREFERRED_LABELS = {
    "documentation", "docs", "good first issue", "good-first-issue",
    "bug", "fix", "help wanted", "easy", "starter",
}

HARD_SKIP_LABELS = {
    "in-progress", "wip", "assigned", "do-not-merge", "blocked",
    "duplicate", "invalid", "wontfix", "question", "discussion",
}


class IssueScorer:
    """
    Multi-signal issue scorer.
    Scores 0–100 on 5 dimensions, then routes to proceed/review/reject.
    """

    def __init__(self):
        self._model  = "llama-3.1-8b-instant"
        self._groq_failed = False
        self._gemini_failed = False
        
        # Groq Setup
        self._groq_key = os.environ.get("GROQ_API_KEY", "")
        self._groq_client = None
        if self._groq_key:
            try:
                import groq as _groq
                self._groq_client = _groq.Groq(api_key=self._groq_key)
            except Exception as e:
                log.warning("scorer.groq_init_failed", error=str(e))

        # Gemini Setup (Fallback)
        self._gemini_key = os.environ.get("GEMINI_API_KEY", "")
        self._gemini_client = None
        if self._gemini_key:
            try:
                import google.generativeai as genai
                genai.configure(api_key=self._gemini_key)
                self._gemini_client = genai.GenerativeModel("gemini-2.0-flash")
            except Exception as e:
                log.warning("scorer.gemini_init_failed", error=str(e))

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
                log.warning("scorer.openrouter_init_failed", error=str(e))

    def score_issue(
        self,
        issue_number: int,
        issue_id: int,
        title: str,
        body: str,
        labels: list[str],
        age_days: int,
        comment_count: int,
        org: str,
        repo: str,
        activity_score: float = 70.0,
        memory: Optional[OrgMemory] = None,
    ) -> IssueScore:
        """
        Score a single issue. Returns an IssueScore with decision.
        """
        score = IssueScore(
            issue_id=issue_id,
            issue_number=issue_number,
            title=title,
            org=org,
            repo=repo,
            age_days=age_days,
            comment_count=comment_count,
            labels=labels,
            activity_score=activity_score,
            model_used=self._model,
        )

        # ── 1. Hard reject checks (no LLM needed) ──────────────
        lower_labels = {lb.lower() for lb in labels}

        # Skip if any hard-skip label present
        for bad in HARD_SKIP_LABELS:
            if bad in lower_labels:
                score.decision = IssueDecision.REJECT
                score.reason   = f"Label '{bad}' indicates ineligible issue"
                log.info("scorer.hard_reject", issue=issue_number, reason=score.reason)
                return score

        # Skip if body is too vague (< 30 chars)
        body_len = len((body or "").strip())
        if body_len < 30:
            score.decision = IssueDecision.REJECT
            score.reason   = "Issue body too short — insufficient context"
            return score

        # Skip if too old (> 90 days with no activity)
        if age_days > 90 and comment_count == 0:
            score.decision = IssueDecision.REJECT
            score.reason   = "Issue is stale (>90 days, no comments)"
            return score

        # ── 2. Label bonus ───────────────────────────────────────
        label_bonus = 0.0
        for lb in lower_labels:
            if lb in PREFERRED_LABELS:
                label_bonus = 100.0
                break

        # ── 3. LLM scoring call ──────────────────────────────────
        llm_scores = self._call_llm_scorer(title, body, labels, memory)

        # ── 4. Weighted composite ────────────────────────────────
        score.clarity_score              = llm_scores.get("clarity", 50)
        score.scope_score                = llm_scores.get("scope", 50)
        score.historical_similarity_score = llm_scores.get("historical_similarity", 50)
        score.testability_score          = llm_scores.get("testability", 50)
        score.activity_score             = activity_score

        overall = (
            WEIGHTS["clarity"]               * score.clarity_score +
            WEIGHTS["scope"]                 * score.scope_score +
            WEIGHTS["historical_similarity"] * score.historical_similarity_score +
            WEIGHTS["testability"]           * score.testability_score +
            WEIGHTS["activity"]              * score.activity_score +
            WEIGHTS["label_bonus"]           * label_bonus
        )
        score.overall_score = round(overall, 1)

        # ── 5. Decision routing ──────────────────────────────────
        if score.overall_score >= DECISION_THRESHOLDS[IssueDecision.PROCEED]:
            score.decision = IssueDecision.PROCEED
            score.reason   = f"Score {score.overall_score:.0f}/100 — strong candidate"
        elif score.overall_score >= DECISION_THRESHOLDS[IssueDecision.MANUAL_REVIEW]:
            score.decision = IssueDecision.MANUAL_REVIEW
            score.reason   = f"Score {score.overall_score:.0f}/100 — borderline, manual check advised"
        else:
            score.decision = IssueDecision.REJECT
            score.reason   = (f"Score {score.overall_score:.0f}/100 — "
                              f"below threshold ({DECISION_THRESHOLDS[IssueDecision.MANUAL_REVIEW]})")

        # Suggested files from LLM
        score.recommended_files = llm_scores.get("likely_files", [])

        log.info("scorer.scored",
                 issue=issue_number,
                 score=score.overall_score,
                 decision=score.decision.value)
        return score

    def _call_llm_scorer(
        self,
        title: str,
        body: str,
        labels: list[str],
        memory: Optional[OrgMemory],
    ) -> dict:
        """
        Ask Llama to score the issue on 4 dimensions.
        Returns dict with numeric scores and file hints.
        """
        memory_context = ""
        if memory:
            if memory.issue_acceptance_patterns:
                memory_context = (
                    f"Previously accepted issue types: "
                    f"{', '.join(memory.issue_acceptance_patterns[:3])}\n"
                )
            if memory.issue_rejection_patterns:
                memory_context += (
                    f"Previously rejected: "
                    f"{', '.join(memory.issue_rejection_patterns[:3])}\n"
                )

        prompt = f"""Score this GitHub issue for automated PR generation.

Title: {title}
Labels: {', '.join(labels) if labels else 'none'}
Body:
{(body or '')[:800]}

{memory_context}

Score each dimension from 0 to 100. Respond with ONLY valid JSON:
{{
  "clarity": <0-100>,
  "scope": <0-100>,
  "historical_similarity": <0-100>,
  "testability": <0-100>,
  "likely_files": ["<filename>", ...]
}}

Scoring guide:
- clarity: How clear is the expected fix? (vague=0, reproducible steps=100)
- scope: How small is the fix? (architecture change=0, one-file typo=100)
- historical_similarity: Does this resemble issues that get merged? (unique=0, common pattern=100)
- testability: Can the fix be validated locally? (needs prod access=0, unit testable=100)
- likely_files: 1-3 probable filenames (can be empty list if unknown)

Be conservative. Bias toward lower scores when uncertain."""

        # 1. Try Groq first
        if self._groq_client and not self._groq_failed:
            try:
                resp = self._groq_client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=300,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                text = resp.choices[0].message.content.strip()
                return json.loads(text)
            except Exception as e:
                log.warning("scorer.groq_error", error=str(e), msg="Falling back to Gemini...")
                self._groq_failed = True

        # 2. Try Gemini fallback
        if self._gemini_client and not self._gemini_failed:
            try:
                resp = self._gemini_client.generate_content(
                    prompt,
                    generation_config={"response_mime_type": "application/json"}
                )
                text = resp.text.strip()
                if text.startswith("```"):
                    text = re.sub(r"^```(?:json)?\n", "", text)
                    text = re.sub(r"\n```$", "", text)
                return json.loads(text)
            except Exception as e:
                log.warning("scorer.gemini_error", error=str(e), msg="Falling back to OpenRouter...")
                self._gemini_failed = True

        # 3. Try OpenRouter fallback
        if self._openrouter_client:
            try:
                resp = self._openrouter_client.chat.completions.create(
                    model="meta-llama/llama-3.1-8b-instruct",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=300,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                text = resp.choices[0].message.content.strip()
                return json.loads(text)
            except Exception as e:
                log.warning("scorer.openrouter_error", error=str(e), msg="LLM scoring failed completely")

        # 4. Static fallback
        return {
            "clarity": 40,
            "scope": 40,
            "historical_similarity": 40,
            "testability": 40,
            "likely_files": [],
        }

    def score_multiple(
        self,
        issues: list[dict],
        org: str,
        repo: str,
        activity_score: float = 70.0,
        memory: Optional[OrgMemory] = None,
    ) -> list[IssueScore]:
        """
        Score a list of issues and return sorted by score descending.
        Stops early if top candidates found.
        """
        results = []
        proceed_count = 0
        for issue_data in issues:
            if proceed_count >= 3:
                log.info("scorer.early_stopping", msg="Found 3 PROCEED candidates, stopping early")
                break
            try:
                score = self.score_issue(
                    issue_number=issue_data["number"],
                    issue_id=issue_data.get("id", 0),
                    title=issue_data.get("title", ""),
                    body=issue_data.get("body", ""),
                    labels=issue_data.get("labels", []),
                    age_days=issue_data.get("age_days", 0),
                    comment_count=issue_data.get("comment_count", 0),
                    org=org,
                    repo=repo,
                    activity_score=activity_score,
                    memory=memory,
                )
                results.append(score)
                if score.decision == IssueDecision.PROCEED:
                    proceed_count += 1
            except Exception as e:
                log.warning("scorer.issue_error",
                            issue=issue_data.get("number"), error=str(e))

        # Sort by overall score descending
        results.sort(key=lambda s: s.overall_score, reverse=True)
        return results

"""
agents/scorer.py — Multi-signal scoring engine for repository activity, issues, and risk.

All formulas are implemented exactly as specified and are deterministic.
The only external call is Llama 3.1 8B on Groq for issue clarity scoring.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import structlog

from memory.config_loader import get_model_name
from memory.schemas import ActivityScore, IssueScore, OrgMemory, RiskScore

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Weight constants — matches the build spec exactly
# ---------------------------------------------------------------------------

ACTIVITY_WEIGHTS = {
    "commit_freshness": 0.40,
    "pr_merge_freshness": 0.30,
    "maintainer_response": 0.20,
    "issue_velocity": 0.10,
}

ISSUE_WEIGHTS = {
    "clarity": 0.25,
    "scope": 0.20,
    "historical_similarity": 0.20,
    "testability": 0.15,
    "activity_score": 0.10,
    "label_bonus": 0.10,
}

RISK_WEIGHTS = {
    "diff_size": 0.35,
    "file_criticality": 0.25,
    "test_coverage_gap": 0.20,
    "confidence_loss": 0.20,
}

# Issue score thresholds
ISSUE_THRESHOLD_PROCEED = 75.0
ISSUE_THRESHOLD_MANUAL = 60.0

# Activity gate threshold
ACTIVITY_THRESHOLD = 60.0


# ---------------------------------------------------------------------------
# Activity scoring
# ---------------------------------------------------------------------------


def compute_activity_score(repo_data: dict) -> ActivityScore:
    """
    Compute a 0–100 repository activity score from raw GitHub signals.

    Signals:
        commit_freshness   = max(0, 100 - days_since_commit * 4)
        pr_merge_freshness = max(0, 100 - days_since_merge * 5)
        maintainer_resp    = mapped avg_review_days → 0-100 (0d=100, 14+d=0)
        issue_velocity     = min(100, max(0, 80 - open_issue_count * 0.5))

    Decision: score >= 60 → proceed, < 60 → skip today.
    """
    days_commit = repo_data.get("last_commit_days", 9999)
    days_merge = repo_data.get("last_merge_days", 9999)
    avg_review = repo_data.get("avg_review_days", 14.0)
    open_count = repo_data.get("open_count", 0)

    commit_freshness = max(0.0, 100.0 - days_commit * 4)
    pr_merge_freshness = max(0.0, 100.0 - days_merge * 5)

    # maintainer_response: 0 days → 100, 14+ days → 0
    maintainer_response = max(0.0, 100.0 - (avg_review / 14.0) * 100.0)

    issue_velocity = min(100.0, max(0.0, 80.0 - open_count * 0.5))

    score = (
        ACTIVITY_WEIGHTS["commit_freshness"] * commit_freshness
        + ACTIVITY_WEIGHTS["pr_merge_freshness"] * pr_merge_freshness
        + ACTIVITY_WEIGHTS["maintainer_response"] * maintainer_response
        + ACTIVITY_WEIGHTS["issue_velocity"] * issue_velocity
    )
    score = round(min(100.0, max(0.0, score)), 2)

    log.info(
        "scorer.activity",
        score=score,
        commit_freshness=commit_freshness,
        pr_merge_freshness=pr_merge_freshness,
        maintainer_response=maintainer_response,
        issue_velocity=issue_velocity,
    )

    return ActivityScore(
        score=score,
        days_since_commit=days_commit,
        days_since_merge=days_merge,
        avg_review_days=avg_review,
        open_issue_count=open_count,
        computed_at=datetime.now(tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Issue scoring
# ---------------------------------------------------------------------------


def compute_issue_score(
    issue: dict,
    org_memory: OrgMemory,
    activity: ActivityScore,
) -> IssueScore:
    """
    Compute a 0–100 eligibility score for a single GitHub issue.

    Components:
        clarity:    Llama 3.1 8B clarity rating via Groq
        scope:      File count estimate from keywords in issue text
        historical: Similarity to accepted PR types in org memory
        testability: Test dir known + issue mentions test/spec
        label_bonus: Good-first-issue/bug/documentation bonus
    """
    body = (issue.get("body") or "").lower()
    title = (issue.get("title") or "").lower()
    labels = [lbl.lower() for lbl in issue.get("labels", [])]
    combined_text = f"{title}\n{body}"

    clarity = _score_clarity(combined_text, issue.get("number", 0))
    scope = _score_scope(combined_text)
    historical = _score_historical(title, org_memory)
    testability = _score_testability(body, org_memory)
    label_bonus = _score_label_bonus(labels)

    raw = (
        ISSUE_WEIGHTS["clarity"] * clarity
        + ISSUE_WEIGHTS["scope"] * scope
        + ISSUE_WEIGHTS["historical_similarity"] * historical
        + ISSUE_WEIGHTS["testability"] * testability
        + ISSUE_WEIGHTS["activity_score"] * activity.score
        + ISSUE_WEIGHTS["label_bonus"] * label_bonus
    )
    score = round(min(100.0, max(0.0, raw)), 2)

    if score >= ISSUE_THRESHOLD_PROCEED:
        decision = "proceed"
        reason = None
    elif score >= ISSUE_THRESHOLD_MANUAL:
        decision = "manual"
        reason = f"Score {score:.0f} is in the manual-review range (60-74)"
    else:
        decision = "reject"
        reason = f"Score {score:.0f} below threshold {ISSUE_THRESHOLD_MANUAL}"

    log.info(
        "scorer.issue",
        issue=issue.get("number"),
        score=score,
        clarity=clarity,
        scope=scope,
        historical=historical,
        testability=testability,
        label_bonus=label_bonus,
        decision=decision,
    )

    return IssueScore(
        issue_number=issue.get("number", 0),
        score=score,
        clarity=clarity,
        scope=scope,
        historical_similarity=historical,
        testability=testability,
        label_bonus=label_bonus,
        decision=decision,
        rejection_reason=reason,
    )


def _score_clarity(text: str, issue_number: int = 0) -> float:
    """
    Use Llama 3.1 8B on Groq to score issue clarity 0–100.

    Prompt: does the issue have clear steps, expected behavior,
    and defined acceptance criteria?

    Fallback chain: Groq → Gemini Flash → heuristic scoring.
    """
    prompt = (
        "Score the clarity of this GitHub issue from 0 to 100.\n"
        "High score means: has clear reproduction steps, describes expected vs actual behavior,\n"
        "has defined acceptance criteria, is not vague or ambiguous.\n"
        "Low score means: one-liner, no context, vague request.\n\n"
        f"Issue text:\n{text[:1500]}\n\n"
        "Respond with only a number between 0 and 100."
    )

    # Try Groq first
    api_key = os.environ.get("GROQ_API_KEY")
    if api_key:
        try:
            from groq import Groq

            client = Groq(api_key=api_key)
            model = get_model_name("scoring_model", "llama-3.1-8b-instant")
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0.0,
            )
            raw = response.choices[0].message.content.strip()
            score = float("".join(c for c in raw if c.isdigit() or c == "."))
            return min(100.0, max(0.0, score))
        except Exception as exc:
            log.warning("scorer.clarity_groq_failed", error=str(exc)[:100], fallback="gemini")

    # Fallback: try Gemini Flash
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            gmodel = genai.GenerativeModel("gemini-2.5-flash")
            response = gmodel.generate_content(prompt)
            raw = response.text.strip()
            score = float("".join(c for c in raw if c.isdigit() or c == "."))
            return min(100.0, max(0.0, score))
        except Exception as exc:
            log.warning("scorer.clarity_gemini_failed", error=str(exc)[:100], fallback="heuristic")

    # Final fallback: heuristic
    log.warning("scorer.clarity_llm_failed", fallback="heuristic")
    return _heuristic_clarity(text)


def _heuristic_clarity(text: str) -> float:
    """Simple heuristic clarity score when Groq is unavailable."""
    score = 30.0  # baseline
    signals = {
        "steps": ["step", "1.", "2.", "first", "then", "finally"],
        "expected": ["expected", "should", "want", "supposed to"],
        "actual": ["actual", "instead", "getting", "bug", "error"],
        "reproduce": ["reproduce", "repro", "to reproduce", "steps to"],
    }
    for category, keywords in signals.items():
        if any(kw in text for kw in keywords):
            score += 17.5
    return min(100.0, score)


def _score_scope(text: str) -> float:
    """
    Estimate how many files need changing from keywords in the issue.

    1-2 files implied → 100
    3-5 files implied → 60
    6+  files implied → 20
    """
    multi_file_signals = [
        "across", "multiple files", "refactor", "all components",
        "entire", "throughout", "everywhere", "each file",
    ]
    few_file_signals = [
        "button", "header", "footer", "one file", "single",
        "typo", "tooltip", "label", "icon", "color",
    ]

    if any(s in text for s in multi_file_signals):
        return 20.0
    if any(s in text for s in few_file_signals):
        return 100.0
    return 60.0  # default — assume moderate scope


def _score_historical(title: str, org_memory: OrgMemory) -> float:
    """
    Compare issue type to accepted/rejected types in org memory.

    Returns high score for issue types that historically get merged,
    low score for types that get rejected.
    """
    pattern = org_memory.pattern_learning
    accepted_types: list[str] = pattern.get("accepted_issue_types", [])
    rejected_types: list[str] = pattern.get("rejected_issue_types", [])

    title_lower = title.lower()

    for t in accepted_types:
        if t.lower() in title_lower:
            return 80.0

    for t in rejected_types:
        if t.lower() in title_lower:
            return 15.0

    return 50.0  # neutral — no pattern found yet


def _score_testability(body: str, org_memory: OrgMemory) -> float:
    """
    Score how testable the issue is.

    High if: test directory exists in org memory AND issue mentions tests/specs.
    """
    test_dir = org_memory.file_knowledge.get("test_directory", "")
    has_test_dir = bool(test_dir)
    mentions_test = any(kw in body for kw in ["test", "spec", "coverage", "assert"])

    if has_test_dir and mentions_test:
        return 85.0
    if has_test_dir:
        return 60.0
    if mentions_test:
        return 45.0
    return 25.0


def _score_label_bonus(labels: list[str]) -> float:
    """
    Apply bonus points based on issue labels.

    good-first-issue → 20  (normalized to 0-100 range for weight application)
    bug              → 15
    documentation    → 10
    else             → 0
    """
    if "good-first-issue" in labels or "good first issue" in labels:
        return 100.0  # full weight in weighted sum → contributes 10 pts
    if "bug" in labels:
        return 75.0
    if "documentation" in labels or "docs" in labels:
        return 50.0
    return 0.0


# ---------------------------------------------------------------------------
# Risk scoring (post-coding)
# ---------------------------------------------------------------------------


def compute_risk_score(
    diff: str,
    files_changed: list[str],
    test_added: bool,
    used_fallback: bool,
    reviewer_flagged: bool = False,
) -> RiskScore:
    """
    Compute a 0–100 risk score shown in the ntfy approval notification.

    Components:
        diff_size:    lines changed bucketed into risk tiers
        file_crit:    core logic > config > tests > docs
        test_gap:     new non-test behavior without a test
        conf_loss:    fallback model used or reviewer flagged issues
    """
    from integrations.git_ops import count_diff_lines

    total_lines = count_diff_lines(diff)

    # Diff size scoring
    if total_lines > 150:
        diff_size_score = 100.0
    elif total_lines > 100:
        diff_size_score = 70.0
    elif total_lines >= 50:
        diff_size_score = 40.0
    else:
        diff_size_score = 15.0

    # File criticality
    file_criticality = _score_file_criticality(files_changed)

    # Test coverage gap
    has_non_test_changes = any(
        "test" not in f.lower() and "spec" not in f.lower()
        for f in files_changed
    )
    if has_non_test_changes and not test_added:
        test_coverage_gap = 80.0
    else:
        test_coverage_gap = 10.0

    # Confidence loss
    if reviewer_flagged and used_fallback:
        confidence_loss = 80.0
    elif reviewer_flagged:
        confidence_loss = 60.0
    elif used_fallback:
        confidence_loss = 40.0
    else:
        confidence_loss = 0.0

    raw = (
        RISK_WEIGHTS["diff_size"] * diff_size_score
        + RISK_WEIGHTS["file_criticality"] * file_criticality
        + RISK_WEIGHTS["test_coverage_gap"] * test_coverage_gap
        + RISK_WEIGHTS["confidence_loss"] * confidence_loss
    )
    score = round(min(100.0, max(0.0, raw)), 2)

    if score <= 30:
        level = "low"
    elif score <= 60:
        level = "medium"
    else:
        level = "high"

    log.info(
        "scorer.risk",
        score=score,
        level=level,
        diff_size_score=diff_size_score,
        file_criticality=file_criticality,
        test_coverage_gap=test_coverage_gap,
        confidence_loss=confidence_loss,
    )

    return RiskScore(
        score=score,
        level=level,
        diff_size_score=diff_size_score,
        file_criticality=file_criticality,
        test_coverage_gap=test_coverage_gap,
        confidence_loss=confidence_loss,
    )


def _score_file_criticality(files: list[str]) -> float:
    """Return the highest criticality score among the changed files."""
    if not files:
        return 0.0
    scores = []
    for f in files:
        fl = f.lower()
        if any(kw in fl for kw in ["test", "spec", "__test__", "mock"]):
            scores.append(20.0)
        elif any(kw in fl for kw in ["readme", ".md", "docs/", "doc/"]):
            scores.append(10.0)
        elif any(kw in fl for kw in [".json", ".yaml", ".yml", ".toml", ".ini", ".env"]):
            scores.append(50.0)
        else:
            scores.append(80.0)  # treat as core logic by default
    return max(scores)

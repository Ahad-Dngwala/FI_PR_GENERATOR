"""
tests/test_scorer.py — Unit tests for the scoring formulas.

All external API calls (Groq) are mocked so tests run offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.scorer import (
    ACTIVITY_THRESHOLD,
    compute_activity_score,
    compute_issue_score,
    compute_risk_score,
    _score_label_bonus,
    _score_scope,
    _quick_filter,
    _parse_batch_json,
    score_issues_batch,
)
from memory.schemas import ActivityScore, OrgMemory, WorkflowRules


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_org_memory() -> OrgMemory:
    activity = ActivityScore(
        score=80.0,
        days_since_commit=2,
        days_since_merge=4,
        avg_review_days=3.0,
        open_issue_count=20,
        computed_at=datetime.now(tz=timezone.utc),
    )
    return OrgMemory(
        org_name="test-org",
        repo_name="test-repo",
        last_refresh=datetime.now(tz=timezone.utc),
        activity=activity,
        conventions={"commit_style": "fix: description", "test_commands": ["pytest"]},
        file_knowledge={"test_directory": "tests/"},
        pattern_learning={
            "accepted_issue_types": ["documentation", "bug"],
            "rejected_issue_types": ["refactor"],
        },
    )


# ---------------------------------------------------------------------------
# Activity score tests
# ---------------------------------------------------------------------------


class TestActivityScore:
    def test_fresh_active_repo_scores_high(self):
        data = {
            "last_commit_days": 1,
            "last_merge_days": 2,
            "avg_review_days": 1.0,
            "open_count": 10,
        }
        result = compute_activity_score(data)
        assert result.score >= 80.0
        assert result.score <= 100.0

    def test_dead_repo_scores_below_threshold(self):
        data = {
            "last_commit_days": 60,
            "last_merge_days": 90,
            "avg_review_days": 20.0,
            "open_count": 200,
        }
        result = compute_activity_score(data)
        assert result.score < ACTIVITY_THRESHOLD

    def test_example_from_readme(self):
        """Test the exact example from the README scoring section."""
        data = {
            "last_commit_days": 2,
            "last_merge_days": 4,
            "avg_review_days": 3.0,
            "open_count": 45,
        }
        result = compute_activity_score(data)
        # README says result should be ~80.55
        assert 78.0 <= result.score <= 83.0

    def test_commit_freshness_formula(self):
        """commit_freshness = max(0, 100 - days * 4)"""
        data = {
            "last_commit_days": 25,  # 100 - 25*4 = 0
            "last_merge_days": 0,
            "avg_review_days": 0.0,
            "open_count": 0,
        }
        result = compute_activity_score(data)
        # commit_freshness should be 0, so score < full weight
        assert result.score < 70.0

    def test_score_clamped_to_0_100(self):
        """Score must always stay in [0, 100]."""
        data = {
            "last_commit_days": 0,
            "last_merge_days": 0,
            "avg_review_days": 0.0,
            "open_count": 0,
        }
        result = compute_activity_score(data)
        assert 0 <= result.score <= 100


# ---------------------------------------------------------------------------
# Issue score tests
# ---------------------------------------------------------------------------


class TestIssueScore:
    @patch("agents.scorer._score_clarity")
    def test_good_issue_scores_above_75(self, mock_clarity, minimal_org_memory):
        mock_clarity.return_value = 90.0
        issue = {
            "number": 42,
            "title": "Fix documentation typo",
            "body": "The README has a typo on line 5. Expected: 'colour', Got: 'color'",
            "labels": ["documentation", "good-first-issue"],
        }
        result = compute_issue_score(issue, minimal_org_memory, minimal_org_memory.activity)
        assert result.score >= 70.0
        assert result.issue_number == 42

    @patch("agents.scorer._score_clarity")
    def test_vague_issue_scores_below_60(self, mock_clarity, minimal_org_memory):
        mock_clarity.return_value = 20.0
        issue = {
            "number": 1,
            "title": "This is broken",
            "body": "pls fix",
            "labels": [],
        }
        result = compute_issue_score(issue, minimal_org_memory, minimal_org_memory.activity)
        assert result.score < 60.0
        assert result.decision == "reject"

    def test_label_bonus_good_first_issue(self):
        score = _score_label_bonus(["good-first-issue"])
        assert score == 100.0

    def test_label_bonus_bug(self):
        score = _score_label_bonus(["bug"])
        assert score == 75.0

    def test_label_bonus_no_labels(self):
        score = _score_label_bonus([])
        assert score == 0.0

    def test_scope_scoring(self):
        assert _score_scope("fix typo in single button") == 100.0
        assert _score_scope("refactor across multiple files") == 20.0
        assert _score_scope("update the component handler") == 60.0


# ---------------------------------------------------------------------------
# Risk score tests
# ---------------------------------------------------------------------------


class TestRiskScore:
    def test_small_safe_diff_is_low_risk(self):
        # 10 added + 5 removed = 15 lines
        diff = "\n".join(["+" + "x" * 10] * 10 + ["-" + "x" * 10] * 5)
        result = compute_risk_score(
            diff=diff,
            files_changed=["tests/test_foo.py"],
            test_added=True,
            used_fallback=False,
        )
        assert result.level == "low"
        assert result.score <= 30.0

    def test_large_diff_is_high_risk(self):
        # 180 added lines
        diff = "\n".join(["+" + "x" * 20] * 180)
        result = compute_risk_score(
            diff=diff,
            files_changed=["src/core/engine.py"],
            test_added=False,
            used_fallback=True,
        )
        assert result.level == "high"

    def test_fallback_model_increases_risk(self):
        diff = "\n".join(["+" + "x"] * 30)
        r_primary = compute_risk_score(diff, ["tests/t.py"], True, False)
        r_fallback = compute_risk_score(diff, ["tests/t.py"], True, True)
        assert r_fallback.score > r_primary.score

    def test_risk_score_clamped(self):
        diff = "\n".join(["+" + "x"] * 500)
        result = compute_risk_score(diff, ["src/main.py"], False, True, True)
        assert 0 <= result.score <= 100


class TestBatchScoring:
    def test_quick_filter_valid_issue(self):
        # Good, active, unassigned issue should pass
        issue = {
            "number": 101,
            "title": "Fix alignment of submit button",
            "body": "The submit button is shifted left on mobile viewport screens.",
            "assignee": None,
            "locked": False,
            "pull_request": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "labels": ["bug", "frontend"],
        }
        passed, reason = _quick_filter(issue)
        assert passed is True
        assert reason == "pass"

    def test_quick_filter_assigned_issue(self):
        # Assigned issues should be dropped
        issue = {
            "number": 102,
            "title": "Fix button",
            "body": "Detailed description of button issue that is longer than 30 chars",
            "assignee": "Ahad-Dngwala",
            "locked": False,
            "pull_request": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "labels": [],
        }
        passed, reason = _quick_filter(issue)
        assert passed is False
        assert reason == "assigned"

    def test_quick_filter_stale_issue(self):
        # Stale issues (>60 days old) should be dropped
        from datetime import timedelta
        stale_date = datetime.now(timezone.utc) - timedelta(days=65)
        issue = {
            "number": 103,
            "title": "Stale bug",
            "body": "Detailed description of button issue that is longer than 30 chars",
            "assignee": None,
            "locked": False,
            "pull_request": None,
            "created_at": stale_date.isoformat(),
            "labels": [],
        }
        passed, reason = _quick_filter(issue)
        assert passed is False
        assert reason == "stale"

    def test_quick_filter_bad_labels(self):
        # Issues with bad labels (wontfix, duplicate, etc.) should be dropped
        issue = {
            "number": 104,
            "title": "Duplicate issue",
            "body": "Detailed description of button issue that is longer than 30 chars",
            "assignee": None,
            "locked": False,
            "pull_request": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "labels": ["duplicate"],
        }
        passed, reason = _quick_filter(issue)
        assert passed is False
        assert reason == "bad_label"

    def test_parse_batch_json_standard(self):
        # Regular JSON should be parsed correctly
        raw = '{"101": 85.0, "102": 45.0}'
        parsed = _parse_batch_json(raw)
        assert parsed == {101: 85.0, 102: 45.0}

    def test_parse_batch_json_with_markdown(self):
        # Markdown JSON block should be parsed correctly
        raw = 'Here is the score:\n```json\n{"101": 85.0, "102": 45.0}\n```\nHope it helps!'
        parsed = _parse_batch_json(raw)
        assert parsed == {101: 85.0, 102: 45.0}

    def test_parse_batch_json_malformed_repair(self):
        # Malformed JSON (no braces, missing quotes, commas) should be repaired via regex fallback
        raw = '"101": 85.0\n"102": 45.0\n103: 90'
        parsed = _parse_batch_json(raw)
        assert parsed == {101: 85.0, 102: 45.0, 103: 90.0}

    @patch("agents.scorer._score_clarity_batch")
    def test_score_issues_batch(self, mock_batch_clarity, minimal_org_memory):
        # Setup mock batch clarity to return scores for the candidates
        mock_batch_clarity.return_value = {101: 90.0}

        issues = [
            {
                "number": 101,
                "title": "Fix alignment",
                "body": "The submit button is shifted left on mobile viewport screens.",
                "assignee": None,
                "locked": False,
                "pull_request": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "labels": ["bug"],
            },
            {
                "number": 102,
                "title": "Assigned issue",
                "body": "This issue is already assigned and should be skipped by pre-filter.",
                "assignee": "someone_else",
                "locked": False,
                "pull_request": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "labels": [],
            }
        ]

        scored = score_issues_batch(issues, minimal_org_memory, minimal_org_memory.activity)
        assert len(scored) == 2
        
        # Candidate should have high score
        candidate_score = next(s for s in scored if s.issue_number == 101)
        assert candidate_score.score >= 60.0
        assert candidate_score.decision == "proceed"

        # Skipped issue should have score of 0.0 and decision reject
        skipped_score = next(s for s in scored if s.issue_number == 102)
        assert skipped_score.score == 0.0
        assert skipped_score.decision == "reject"
        assert "Heuristic filter" in skipped_score.rejection_reason

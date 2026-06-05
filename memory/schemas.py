"""
memory/schemas.py — All Pydantic v2 data models for FI-PR-GENERATOR.

Every data structure that crosses a boundary (disk, API, agent-to-agent)
is defined here. No bare dicts in business logic — use these models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Activity & Scoring
# ---------------------------------------------------------------------------


class ActivityScore(BaseModel):
    """Repository-level activity health score (0–100)."""

    score: float = Field(..., ge=0, le=100, description="Weighted composite score")
    days_since_commit: int
    days_since_merge: int
    avg_review_days: float
    open_issue_count: int
    computed_at: datetime


class IssueScore(BaseModel):
    """Per-issue eligibility score produced by agents/scorer.py."""

    issue_number: int
    score: float = Field(..., ge=0, le=100)
    clarity: float = Field(..., ge=0, le=100)
    scope: float = Field(..., ge=0, le=100)
    historical_similarity: float = Field(..., ge=0, le=100)
    testability: float = Field(..., ge=0, le=100)
    label_bonus: float = Field(..., ge=0, le=100)
    decision: Literal["proceed", "manual", "reject"]
    rejection_reason: Optional[str] = None


class RiskScore(BaseModel):
    """Post-coding risk assessment shown in ntfy approval notification."""

    score: float = Field(..., ge=0, le=100)
    level: Literal["low", "medium", "high"]
    diff_size_score: float
    file_criticality: float
    test_coverage_gap: float
    confidence_loss: float


# ---------------------------------------------------------------------------
# Org Memory
# ---------------------------------------------------------------------------


class RejectionEntry(BaseModel):
    """Records a human or system rejection for pattern learning."""

    rejected_at: datetime
    issue_number: int
    issue_type: str
    diff_size_lines: int
    rejection_reason: str
    rejected_by: Literal["human", "scope_guard", "test_failure", "system"]


class WorkflowRules(BaseModel):
    """
    Dynamically discovered contribution workflow for a specific repo.

    NOT predefined — inferred by memory_builder via LLM analysis of
    CONTRIBUTING.md, bot comments, issue/PR templates, and maintainer
    comment patterns. Stored and evolves per repo.

    raw_patterns captures any novel patterns the LLM discovers that do not
    map neatly to the known fields above. This makes the system open-ended:
    new contribution styles get recorded rather than ignored.
    """

    # Known workflow variants (all optional — None means "not detected")
    assignment_required: Optional[bool] = None
    claim_bot_present: Optional[bool] = None
    claim_command: Optional[str] = None          # e.g. "/claim", "!take"
    proposal_required: Optional[bool] = None     # must explain approach first
    direct_pr_allowed: Optional[bool] = None     # no issue needed
    pr_template_required: Optional[bool] = None
    issue_template_required: Optional[bool] = None
    bot_assigns: Optional[bool] = None           # bot auto-assigns on /claim
    maintainer_assigns: Optional[bool] = None    # human maintainer assigns
    self_assign_allowed: Optional[bool] = None   # /assign to self

    # Free-form patterns LLM detected but don't fit known fields
    raw_patterns: list[dict] = Field(
        default_factory=list,
        description="Novel patterns discovered by LLM — open-ended list",
    )

    # Metadata
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    last_inferred_at: Optional[datetime] = None
    inferred_from: list[str] = Field(
        default_factory=list,
        description="Sources used: CONTRIBUTING.md, issue_template, bot_comments, etc.",
    )


class OrgMemory(BaseModel):
    """
    Per-repository knowledge base.

    Stored at memory_store/{org}/{repo}.json
    Schema version bumped on every incompatible change.
    """

    schema_version: int = 1
    org_name: str
    repo_name: str
    last_refresh: datetime
    refresh_frequency_hours: int = 48
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    activity: ActivityScore

    # Repository conventions extracted from merged PR history
    conventions: dict = Field(
        default_factory=dict,
        description=(
            "commit_style, branch_naming, test_commands, build_command, "
            "package_manager, primary_language"
        ),
    )

    # File-level knowledge
    file_knowledge: dict = Field(
        default_factory=dict,
        description="hotspots, test_dir, config_files",
    )

    # Learned contribution patterns
    pattern_learning: dict = Field(
        default_factory=dict,
        description=(
            "accepted_issue_types, rejected_issue_types, "
            "accepted_pr_size_avg_lines, maintainer_preferences"
        ),
    )

    # LLM-inferred workflow rules (dynamic, open-ended)
    workflow_rules: WorkflowRules = Field(default_factory=WorkflowRules)

    # Historical rejection log for re-training future issue scoring
    rejection_log: list[RejectionEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Run State (pipeline persistence)
# ---------------------------------------------------------------------------


class RunState(BaseModel):
    """
    Serialisable state for a single pipeline run.

    Written to state/{run_id}.json after every transition.
    Enables crash recovery, audit logging, and ntfy callback resolution.
    """

    run_id: str
    state: Literal[
        "idle",
        "selecting",
        "checking",
        "planning",
        "retrieving",
        "coding",
        "reviewing",
        "testing",
        "waiting_approval",
        "pushing",
        "drafting_pr",
        "completed",
        "blocked",
        "failed",
    ]
    org: str
    repo: str
    issue_number: Optional[int] = None
    branch: Optional[str] = None
    diff_path: Optional[str] = None
    test_result: Optional[str] = None
    risk_score: Optional[RiskScore] = None
    retry_count: int = 0
    model_used: Optional[str] = None
    failure_reason: Optional[str] = None
    last_claim_attempt: Optional[datetime] = None
    claim_attempt_count: int = 0
    claim_cooldown_seconds: int = 300
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Config schemas (mirrors config/orgs.json and config/models.json)
# ---------------------------------------------------------------------------


class RepoConfig(BaseModel):
    """Per-repository configuration inside orgs.json."""

    name: str
    enabled: bool = True
    mode: Literal["org", "selected", "deep"] = "deep"
    max_prs_per_day: int = 1
    allowed_issue_types: list[str] = Field(
        default_factory=list,
        description=(
            "Issue label types to target. Empty list = scan ALL open issues (recommended). "
            "Add labels like ['documentation', 'bug', 'frontend'] to restrict to those labels only. "
            "Note: this is a preference hint for scoring, not a hard filter — "
            "issues without these labels still get scored but get zero label_bonus."
        ),
    )
    assignment_required: bool = True
    max_repo_size_mb: int = 200
    max_open_issues_scan: int = 50


class OrgConfig(BaseModel):
    """Single organisation entry in orgs.json."""

    name: str
    repos: list[RepoConfig] = Field(default_factory=list)
    skip: bool = False


class GlobalConfig(BaseModel):
    """Root structure of config/orgs.json."""

    orgs: list[OrgConfig] = Field(default_factory=list)
    issue_score_threshold: int = 60
    max_diff_lines: int = 200
    max_retries: int = 2

"""
FI-PR-GENERATOR — Data Schemas
All Pydantic models for structured data throughout the pipeline.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────

class FailureClass(str, Enum):
    CODE_BUG      = "code_bug"
    ENV_ISSUE     = "env_issue"
    FLAKY         = "flaky"
    PREEXISTING   = "preexisting"
    UNRELATED     = "unrelated"
    UNKNOWN       = "unknown"


class IssueDecision(str, Enum):
    PROCEED       = "proceed"
    MANUAL_REVIEW = "manual_review"
    REJECT        = "reject"


class ApprovalDecision(str, Enum):
    APPROVED  = "approved"
    REJECTED  = "rejected"
    REVISE    = "revise"
    TIMEOUT   = "timeout"


class PipelineState(str, Enum):
    IDLE              = "idle"
    SELECTING         = "selecting"
    CHECKING          = "checking"
    PLANNING          = "planning"
    RETRIEVING        = "retrieving"
    CODING            = "coding"
    REVIEWING         = "reviewing"
    TESTING           = "testing"
    WAITING_APPROVAL  = "waiting_approval"
    PUSHING           = "pushing"
    DRAFTING_PR       = "drafting_pr"
    BLOCKED           = "blocked"
    FAILED            = "failed"
    COMPLETED         = "completed"


# ─────────────────────────────────────────────────────────────
# Org Memory Schema
# ─────────────────────────────────────────────────────────────

class OrgMemory(BaseModel):
    """Versioned per-repository knowledge base."""
    version: int = 1
    org_name: str
    repo_name: str
    default_branch: str = "main"
    maintainers: list[str] = Field(default_factory=list)

    # Activity
    activity_score: float = 0.0
    last_commit_at: Optional[str] = None
    last_merged_pr_at: Optional[str] = None

    # Patterns learned from merged PRs
    commit_style: str = ""                    # e.g. "fix: lowercase imperative"
    pr_title_style: str = ""                  # e.g. "fix(component): description"
    common_test_commands: list[str] = Field(default_factory=list)
    common_lint_commands: list[str] = Field(default_factory=list)
    common_build_commands: list[str] = Field(default_factory=list)
    common_file_hotspots: list[str] = Field(default_factory=list)
    common_issue_labels: list[str] = Field(default_factory=list)

    # Acceptance/rejection patterns
    issue_acceptance_patterns: list[str] = Field(default_factory=list)
    issue_rejection_patterns: list[str] = Field(default_factory=list)
    accepted_pr_examples: list[dict] = Field(default_factory=list)  # max 3

    # Human rejection log (improves future scoring)
    rejection_log: list[dict] = Field(default_factory=list)

    # Review behavior
    review_turnaround_days: float = 7.0
    require_assignment: bool = True

    # Metadata
    last_refresh: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    confidence: float = 0.5          # 0.0 – 1.0
    notes: str = ""
    source_links: list[str] = Field(default_factory=list)
    status: str = "active"           # active | dormant | hostile


# ─────────────────────────────────────────────────────────────
# Issue Score Schema
# ─────────────────────────────────────────────────────────────

class IssueScore(BaseModel):
    """Scored GitHub issue record."""
    issue_id: int
    issue_number: int
    title: str
    org: str
    repo: str

    # Component scores (0–100)
    clarity_score: float = 0.0
    scope_score: float = 0.0
    historical_similarity_score: float = 0.0  # replaces "acceptance probability"
    testability_score: float = 0.0
    activity_score: float = 0.0

    # Weighted composite
    overall_score: float = 0.0

    # Issue metadata
    age_days: int = 0
    comment_count: int = 0
    assignment_status: str = "unassigned"
    locked_status: bool = False
    stale_status: bool = False
    labels: list[str] = Field(default_factory=list)

    # Decision
    decision: IssueDecision = IssueDecision.REJECT
    reason: str = ""
    recommended_files: list[str] = Field(default_factory=list)

    # Audit
    model_used: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─────────────────────────────────────────────────────────────
# Validation / Test Result Schema
# ─────────────────────────────────────────────────────────────

class ValidationResult(BaseModel):
    """Result of a local test/lint/type-check run."""
    command: str
    exit_code: int
    stdout_summary: str = ""
    stderr_summary: str = ""
    failure_class: FailureClass = FailureClass.UNKNOWN
    retry_count: int = 0
    environment_notes: str = ""
    preexisting_notes: str = ""
    result: str = "unknown"       # pass | fail | skip
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    repo: str = ""
    branch: str = ""


# ─────────────────────────────────────────────────────────────
# Approval Message Schema
# ─────────────────────────────────────────────────────────────

class ApprovalMessage(BaseModel):
    """Payload sent to Telegram for human approval."""
    title: str
    status: str
    changed_files: list[str]
    diff_preview: str = ""          # first 30 lines of diff
    test_summary: str = ""
    risk_score: float = 0.0         # 0–100
    risk_level: str = "low"         # low | medium | high
    approval_needed: bool = True
    issue_link: str = ""
    branch: str = ""
    next_action: str = "Approve to push draft PR"
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─────────────────────────────────────────────────────────────
# Task / Pipeline State
# ─────────────────────────────────────────────────────────────

class TaskState(BaseModel):
    """Serializable pipeline state — survives restarts."""
    task_id: str
    state: PipelineState = PipelineState.IDLE

    # Target
    org: str = ""
    repo: str = ""
    issue_number: int = 0
    issue_title: str = ""
    issue_url: str = ""

    # Branch
    branch_name: str = ""
    base_commit: str = ""

    # Artifacts
    issue_score: Optional[IssueScore] = None
    retrieved_files: list[str] = Field(default_factory=list)
    retrieval_confidence: float = 0.0
    diff_content: str = ""
    diff_line_count: int = 0
    validation_result: Optional[ValidationResult] = None
    approval_decision: Optional[ApprovalDecision] = None
    pr_url: str = ""

    # Retry tracking
    retry_count: int = 0
    model_used: str = ""

    # Errors
    blocker: str = ""
    failure_class: Optional[FailureClass] = None

    # Timestamps
    started_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    completed_at: Optional[str] = None

    def advance(self, new_state: PipelineState) -> None:
        self.state = new_state
        self.updated_at = datetime.utcnow().isoformat()

    def fail(self, blocker: str, failure_class: FailureClass = FailureClass.UNKNOWN) -> None:
        self.state = PipelineState.FAILED
        self.blocker = blocker
        self.failure_class = failure_class
        self.updated_at = datetime.utcnow().isoformat()

from datetime import datetime, timezone
import pytest

from orchestrator import determine_next_workflow_action
from memory.schemas import WorkflowRules


def test_determine_next_workflow_action_direct_pr():
    rules = WorkflowRules(direct_pr_allowed=True)
    action = determine_next_workflow_action(rules, 10, "testuser")
    assert action == "SKIP_ASSIGNMENT"


def test_determine_next_workflow_action_no_assign_required():
    rules = WorkflowRules(assignment_required=False)
    action = determine_next_workflow_action(rules, 10, "testuser")
    assert action == "SKIP_ASSIGNMENT"


def test_determine_next_workflow_action_auto_claim():
    rules = WorkflowRules(self_assign_allowed=True, claim_command="/claim")
    action = determine_next_workflow_action(rules, 10, "testuser")
    assert action == "AUTO_CLAIM"


def test_determine_next_workflow_action_proposal_required():
    rules = WorkflowRules(proposal_required=True, assignment_required=True)
    action = determine_next_workflow_action(rules, 10, "testuser")
    assert action == "REQUEST_DISCUSSION"


def test_determine_next_workflow_action_wait_for_assignment():
    rules = WorkflowRules(assignment_required=True)
    action = determine_next_workflow_action(rules, 10, "testuser")
    assert action == "WAIT_FOR_ASSIGNMENT"

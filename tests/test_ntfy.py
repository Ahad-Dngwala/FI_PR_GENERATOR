"""
tests/test_ntfy.py — Tests for the ntfy.sh notification + Flask approval server.

Tests are split into three categories:
  1. Unit tests (no network, no keys) — test Flask server routes and file I/O
  2. Integration tests (require NTFY_TOPIC) — send a real notification to ntfy.sh
  3. Approval flow tests — full send→wait→respond cycle using a real Flask server

WHERE DOES THE MOBILE NOTIFICATION APPEAR?
  - Install the "ntfy" app from Google Play / Apple App Store
  - Open the app → Add subscription → enter your NTFY_TOPIC value
  - When a notification is sent, it appears on your phone like a push notification
  - The notification has two action buttons: [Approve] and [Reject]
  - Tapping a button sends a POST request to APPROVAL_SERVER_URL/approve/{run_id}
    or APPROVAL_SERVER_URL/reject/{run_id}
  - The pipeline's wait_for_approval() polls for this response file

WHERE DOES THE APPROVAL GO?
  - The Flask server (running on localhost:8080) receives the POST
  - It writes state/{run_id}_approval.json with {"approved": true/false}
  - wait_for_approval() reads this file and returns True/False to the pipeline

HOW TO SEE IT ON PHONE (requires tunnel):
  1. Set NTFY_TOPIC=your-unique-topic in .env
  2. Run: ngrok http 8080  (or: cloudflared tunnel --url http://localhost:8080)
  3. Set APPROVAL_SERVER_URL=https://your-ngrok-url.ngrok.io in .env
  4. Run this test file — you will get a real notification on your phone
  5. Tap Approve or Reject and watch the test pass

Run: python -m pytest tests/test_ntfy.py -v -s
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
import requests

# State dir used by the approval server
STATE_DIR = Path("state")


# ===========================================================================
# PART 1 — Unit Tests (no network required)
# ===========================================================================


class TestApprovalFileIO:
    """Test the state file I/O logic without starting any server."""

    def test_write_approval_creates_file(self, tmp_path, monkeypatch):
        """_write_approval should create a JSON file with the decision."""
        monkeypatch.setattr(
            "integrations.ntfy_notifier.STATE_DIR", tmp_path
        )
        from integrations.ntfy_notifier import _write_approval

        _write_approval("test-run-001", approved=True)
        expected = tmp_path / "test-run-001_approval.json"
        assert expected.exists(), "Approval state file was not created"
        data = json.loads(expected.read_text())
        assert data["approved"] is True
        assert data["run_id"] == "test-run-001"

    def test_write_rejection_creates_file(self, tmp_path, monkeypatch):
        """_write_approval with approved=False should record rejection."""
        monkeypatch.setattr(
            "integrations.ntfy_notifier.STATE_DIR", tmp_path
        )
        from integrations.ntfy_notifier import _write_approval

        _write_approval("test-run-002", approved=False)
        data = json.loads((tmp_path / "test-run-002_approval.json").read_text())
        assert data["approved"] is False

    def test_wait_for_approval_reads_file(self, tmp_path, monkeypatch):
        """wait_for_approval should return True when file exists with approved=True."""
        monkeypatch.setattr(
            "integrations.ntfy_notifier.STATE_DIR", tmp_path
        )
        from integrations.ntfy_notifier import wait_for_approval

        # Pre-write the approval file (simulating human tapping Approve)
        approval_file = tmp_path / "run-abc_approval.json"
        approval_file.write_text(json.dumps({"run_id": "run-abc", "approved": True}))

        result = wait_for_approval("run-abc", timeout_minutes=1)
        assert result is True

    def test_wait_for_approval_returns_false_on_rejection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "integrations.ntfy_notifier.STATE_DIR", tmp_path
        )
        from integrations.ntfy_notifier import wait_for_approval

        approval_file = tmp_path / "run-def_approval.json"
        approval_file.write_text(json.dumps({"run_id": "run-def", "approved": False}))

        result = wait_for_approval("run-def", timeout_minutes=1)
        assert result is False

    def test_wait_for_approval_timeout_returns_none(self, tmp_path, monkeypatch):
        """wait_for_approval must return None (not True) on timeout."""
        monkeypatch.setattr(
            "integrations.ntfy_notifier.STATE_DIR", tmp_path
        )
        from integrations.ntfy_notifier import wait_for_approval

        # No file written — simulate human not responding
        # Use a very short timeout (1/60 of a minute = 1 second)
        result = wait_for_approval("run-timeout-test", timeout_minutes=1 / 60)
        assert result is None, (
            "Timeout must return None — silence is NOT approval! "
            "Got: " + str(result)
        )


# ===========================================================================
# PART 2 — Flask Approval Server Tests
# ===========================================================================


class TestFlaskApprovalServer:
    """
    Start the Flask approval server and test the HTTP routes.

    The server runs on a random port in tests to avoid conflicts with
    a production server that might be running on 8080.
    """

    @pytest.fixture(autouse=True)
    def start_server(self, tmp_path, monkeypatch):
        """Start the approval server on a test port before each test."""
        monkeypatch.setattr("integrations.ntfy_notifier.STATE_DIR", tmp_path)
        self.tmp_path = tmp_path
        self.test_port = 18080  # use a non-standard port for tests

        # Start the server (daemon thread — stops when test process exits)
        from integrations.ntfy_notifier import start_approval_server
        start_approval_server(port=self.test_port)
        time.sleep(0.8)  # brief wait for Flask to bind

    def test_health_endpoint(self):
        """GET /health should return 200 and status: ok."""
        resp = requests.get(f"http://localhost:{self.test_port}/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_approve_endpoint_creates_file(self):
        """POST /approve/{run_id} should create the approval file."""
        run_id = "flask-test-approve"
        resp = requests.post(
            f"http://localhost:{self.test_port}/approve/{run_id}", timeout=5
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        approval_file = self.tmp_path / f"{run_id}_approval.json"
        assert approval_file.exists()
        data = json.loads(approval_file.read_text())
        assert data["approved"] is True
        print(f"\n  Approve endpoint OK. File: {approval_file.name}")

    def test_reject_endpoint_creates_file(self):
        """POST /reject/{run_id} should create the rejection file."""
        run_id = "flask-test-reject"
        resp = requests.post(
            f"http://localhost:{self.test_port}/reject/{run_id}", timeout=5
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

        rejection_file = self.tmp_path / f"{run_id}_approval.json"
        assert rejection_file.exists()
        data = json.loads(rejection_file.read_text())
        assert data["approved"] is False
        print(f"\n  Reject endpoint OK. File: {rejection_file.name}")

    def test_full_approve_flow(self):
        """
        Full flow: start wait, simulate phone tap Approve, verify True returned.

        This simulates what happens when the human taps [Approve] on their phone:
          1. Pipeline calls wait_for_approval() and blocks
          2. ntfy.sh delivers notification to phone
          3. Human taps Approve
          4. Phone's ntfy app POSTs to /approve/{run_id}
          5. Flask writes state file
          6. wait_for_approval() reads file and returns True
        """
        run_id = "full-flow-approve"

        from integrations.ntfy_notifier import wait_for_approval

        # Simulate the "phone tap" 0.5 seconds after wait starts
        def _simulate_phone_tap():
            time.sleep(0.5)
            requests.post(
                f"http://localhost:{self.test_port}/approve/{run_id}", timeout=5
            )

        tap_thread = threading.Thread(target=_simulate_phone_tap, daemon=True)
        tap_thread.start()

        # This blocks until the file appears (up to 1 minute)
        result = wait_for_approval(run_id, timeout_minutes=1)
        assert result is True, "Expected True from wait_for_approval after Approve tap"
        print("\n  Full approve flow: PASSED (phone tap simulated)")

    def test_full_reject_flow(self):
        """Full flow: simulate phone tap Reject, verify False returned."""
        run_id = "full-flow-reject"

        from integrations.ntfy_notifier import wait_for_approval

        def _simulate_phone_tap():
            time.sleep(0.5)
            requests.post(
                f"http://localhost:{self.test_port}/reject/{run_id}", timeout=5
            )

        tap_thread = threading.Thread(target=_simulate_phone_tap, daemon=True)
        tap_thread.start()

        result = wait_for_approval(run_id, timeout_minutes=1)
        assert result is False, "Expected False from wait_for_approval after Reject tap"
        print("\n  Full reject flow: PASSED (phone tap simulated)")


# ===========================================================================
# PART 3 — Real ntfy.sh Notification Test (requires NTFY_TOPIC)
# ===========================================================================


class TestNtfyNotification:
    """
    Send a real notification to ntfy.sh.

    SKIPPED if NTFY_TOPIC is not set.

    After running this test, you should see a notification on your phone
    in the ntfy app — subscribe to the topic in NTFY_TOPIC first.
    """

    @pytest.fixture(autouse=True)
    def check_env(self):
        if not os.environ.get("NTFY_TOPIC"):
            pytest.skip("NTFY_TOPIC not set — add to .env to receive real mobile notification")

    def test_send_notification_to_phone(self):
        """
        Send a test notification to your ntfy topic.

        HOW TO SEE IT:
          1. Install ntfy app (Android/iOS)
          2. Subscribe to topic: value of NTFY_TOPIC in your .env
          3. Run this test
          4. You should see: 'FI-PR-GENERATOR: Test Notification'

        The notification will have [Approve] and [Reject] buttons.
        Tapping them sends a POST to APPROVAL_SERVER_URL (which must be
        publicly reachable — use ngrok for local development).
        """
        from integrations.ntfy_notifier import ApprovalRequest, send_approval_request

        req = ApprovalRequest(
            run_id="test-notification-001",
            org="test-org",
            repo="test-repo",
            issue_number=999,
            issue_title="Test Notification from fi-pr-generator",
            branch="fix/test-branch-999",
            files_changed=["src/test_file.py"],
            diff_summary="  src/test_file.py (+5/-2)",
            test_result="PASS",
            risk_level="low",
            risk_score=15.0,
            reviewer_notes=["This is a TEST notification. Tap Approve to confirm it works."],
            model_used="gemini/gemini-2.5-pro",
        )

        topic = os.environ["NTFY_TOPIC"]
        ntfy_url = os.environ.get("NTFY_URL", "https://ntfy.sh")
        server_url = os.environ.get("APPROVAL_SERVER_URL", "http://localhost:8080")

        print(f"\n  Sending notification to: {ntfy_url}/{topic}")
        print(f"  Approval server URL: {server_url}")
        print(f"  Expected: notification appears in ntfy app on topic '{topic}'")
        print(f"  Action buttons point to: {server_url}/approve/test-notification-001")

        sent = send_approval_request(
            req,
            ntfy_url=ntfy_url,
            topic=topic,
            server_url=server_url,
            token=os.environ.get("NTFY_TOKEN", ""),
        )
        assert sent, (
            f"Failed to send notification to ntfy.sh/{topic}. "
            "Check NTFY_TOPIC and NTFY_URL in your .env"
        )
        print(f"\n  Notification SENT to {ntfy_url}/{topic}")
        print("  Check your phone for the notification!")
        print("  Subscribe in ntfy app to topic: " + topic)

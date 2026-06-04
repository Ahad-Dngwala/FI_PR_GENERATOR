"""
integrations/ntfy_notifier.py — ntfy.sh push notification + approval server.

Sends rich mobile notifications when a patch is ready for human review.
Provides action buttons (Approve / Reject) that POST back to a local Flask server.
The pipeline blocks until the human responds or the timeout expires.

NOTE: For phone approval to work, APPROVAL_SERVER_URL must be publicly reachable.
      On Windows localhost, use ngrok or Cloudflare Tunnel:
        ngrok http 8080  → set APPROVAL_SERVER_URL=https://xxxx.ngrok.io
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
import structlog

log = structlog.get_logger(__name__)

STATE_DIR = Path("state")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRequest:
    """All information shown in the ntfy approval notification."""

    run_id: str
    org: str
    repo: str
    issue_number: int
    issue_title: str
    branch: str
    files_changed: list[str]
    diff_summary: str        # e.g. "+12/-3 src/Navbar.tsx\n+8/-0 tests/Navbar.test.ts"
    test_result: str         # PASS / ENV_ISSUE / PREEXISTING / etc.
    risk_level: str          # low / medium / high
    risk_score: float        # 0-100
    reviewer_notes: list[str] = field(default_factory=list)
    model_used: str = "gemini/gemini-2.5-pro"


# ---------------------------------------------------------------------------
# Notification sender
# ---------------------------------------------------------------------------

_RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}
_TEST_EMOJI = {"PASS": "✅", "ENV_ISSUE": "⚠️", "PREEXISTING": "📝", "FLAKY": "🎲"}


def _build_body(req: ApprovalRequest) -> str:
    """Format the ntfy notification body."""
    risk_icon = _RISK_EMOJI.get(req.risk_level, "⚪")
    test_icon = _TEST_EMOJI.get(req.test_result, "❓")
    files_str = req.diff_summary or "\n".join(req.files_changed[:5])
    notes_str = ""
    if req.reviewer_notes:
        notes_str = "\nNotes: " + " | ".join(req.reviewer_notes[:3])

    return (
        f"Repo: {req.org}/{req.repo}\n"
        f"Branch: {req.branch}\n"
        f"Files:\n{files_str}\n"
        f"Tests: {test_icon} {req.test_result}\n"
        f"Risk: {risk_icon} {req.risk_level.upper()} ({req.risk_score:.0f}/100)\n"
        f"Model: {req.model_used}"
        f"{notes_str}"
    )


def send_approval_request(
    req: ApprovalRequest,
    ntfy_url: Optional[str] = None,
    topic: Optional[str] = None,
    server_url: Optional[str] = None,
    token: Optional[str] = None,
) -> bool:
    """
    POST the approval request to ntfy.sh.

    ntfy action buttons allow the human to approve/reject directly from their phone.
    Returns True if the notification was sent successfully, False otherwise.
    """
    ntfy_url = ntfy_url or os.environ.get("NTFY_URL", "https://ntfy.sh")
    topic = topic or os.environ.get("NTFY_TOPIC", "fi-pr-notifications")
    server_url = server_url or os.environ.get("APPROVAL_SERVER_URL", "http://localhost:8080")
    token = token or os.environ.get("NTFY_TOKEN", "")

    title = f"FI-PR: #{req.issue_number} — {req.issue_title[:50]}"
    body = _build_body(req)
    priority = 4 if req.risk_level == "high" else 3
    risk_tag = _RISK_EMOJI.get(req.risk_level, 'white_circle').replace('🟢', 'green_circle').replace('🟡', 'yellow_circle').replace('🔴', 'red_circle')

    payload = {
        "topic": topic,
        "message": body,
        "title": title,
        "priority": priority,
        "tags": ["robot", risk_tag],
        "actions": [
            {
                "action": "view",
                "label": "🔍 View Diff",
                "url": f"{server_url.rstrip('/')}/diff/{req.run_id}"
            },
            {
                "action": "http",
                "label": "✅ Approve",
                "url": f"{server_url.rstrip('/')}/approve/{req.run_id}",
                "method": "POST",
                "clear": True
            },
            {
                "action": "http",
                "label": "❌ Reject",
                "url": f"{server_url.rstrip('/')}/reject/{req.run_id}",
                "method": "POST",
                "clear": True
            }
        ]
    }

    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = ntfy_url.rstrip('/')
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        log.info(
            "ntfy.notification_sent",
            run_id=req.run_id,
            topic=topic,
            issue=req.issue_number,
            status=resp.status_code,
        )
        return True
    except requests.RequestException as exc:
        log.error("ntfy.send_failed", run_id=req.run_id, error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Approval Flask server
# ---------------------------------------------------------------------------

_flask_thread: Optional[threading.Thread] = None


def start_approval_server(port: int = 8080) -> None:
    """
    Start a minimal Flask approval server in a daemon background thread.

    Routes:
        POST /approve/{run_id}  → writes {run_id}.json with approved=True
        POST /reject/{run_id}   → writes {run_id}.json with approved=False

    Only starts one server per process (idempotent).
    """
    global _flask_thread
    if _flask_thread is not None and _flask_thread.is_alive():
        log.info("ntfy.server_already_running", port=port)
        return

    def _run_server():
        try:
            from flask import Flask, jsonify

            app = Flask("fi_pr_approval")
            # Suppress Flask's default noisy startup logs
            import logging as _logging
            _logging.getLogger("werkzeug").setLevel(_logging.ERROR)

            @app.post("/approve/<run_id>")
            def approve(run_id: str):
                _write_approval(run_id, approved=True)
                log.info("ntfy.approved", run_id=run_id)
                return jsonify({"status": "approved", "run_id": run_id})

            @app.post("/reject/<run_id>")
            def reject(run_id: str):
                _write_approval(run_id, approved=False)
                log.info("ntfy.rejected", run_id=run_id)
                return jsonify({"status": "rejected", "run_id": run_id})

            @app.get("/diff/<run_id>")
            def show_diff(run_id: str):
                import html
                diff_file = Path("diffs") / f"{run_id}.diff"
                if not diff_file.exists():
                    return "Diff not found on server. It might have been deleted.", 404
                try:
                    diff_content = diff_file.read_text(encoding="utf-8")
                except Exception as e:
                    return f"Error reading diff: {e}", 500

                lines = diff_content.splitlines()
                formatted_lines = []
                for line in lines:
                    escaped_line = html.escape(line)
                    if line.startswith("+") and not line.startswith("+++"):
                        formatted_lines.append(f'<span style="color:#3fb950;background-color:rgba(46,160,67,0.15);display:block;">{escaped_line}</span>')
                    elif line.startswith("-") and not line.startswith("---"):
                        formatted_lines.append(f'<span style="color:#f85149;background-color:rgba(248,81,73,0.15);display:block;">{escaped_line}</span>')
                    elif line.startswith("@@") or line.startswith("diff ") or line.startswith("index "):
                        formatted_lines.append(f'<span style="color:#8b949e;display:block;font-weight:bold;">{escaped_line}</span>')
                    else:
                        formatted_lines.append(f'<span style="display:block;">{escaped_line}</span>')
                
                formatted_html = "\n".join(formatted_lines)
                
                return f"""<!DOCTYPE html>
<html>
<head>
    <title>PR Diff - {run_id}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            background-color: #0d1117;
            color: #c9d1d9;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 20px;
        }}
        h2 {{
            color: #58a6ff;
            border-bottom: 1px solid #30363d;
            padding-bottom: 10px;
            margin-top: 0;
        }}
        pre {{
            background-color: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 16px;
            overflow-x: auto;
            font-family: ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace;
            font-size: 13px;
            line-height: 1.5;
        }}
    </style>
</head>
<body>
    <h2>PR Patch Diff ({run_id})</h2>
    <pre><code>{formatted_html}</code></pre>
</body>
</html>
"""

            @app.get("/health")
            def health():
                return jsonify({"status": "ok"})

            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
        except Exception as exc:
            log.error("ntfy.server_crashed", error=str(exc))

    _flask_thread = threading.Thread(target=_run_server, daemon=True, name="approval-server")
    _flask_thread.start()
    time.sleep(0.5)  # Brief wait for server to bind
    log.info("ntfy.server_started", port=port)


def wait_for_approval(
    run_id: str,
    timeout_minutes: int = 60,
) -> Optional[bool]:
    """
    Poll the approval state file every 10 seconds until a decision is recorded.

    Returns:
        True  = approved
        False = rejected
        None  = timeout (do NOT treat silence as approval)

    On timeout the state file is preserved — the human can still approve later
    by resuming the pipeline manually.
    """
    deadline = time.monotonic() + timeout_minutes * 60
    state_file = STATE_DIR / f"{run_id}_approval.json"

    log.info("ntfy.waiting_for_approval", run_id=run_id, timeout_minutes=timeout_minutes)

    while time.monotonic() < deadline:
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                approved = data.get("approved")
                if approved is not None:
                    state_file.unlink(missing_ok=True)  # Clean up
                    log.info("ntfy.decision_received", run_id=run_id, approved=approved)
                    return bool(approved)
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(10)

    log.warning("ntfy.approval_timeout", run_id=run_id, timeout_minutes=timeout_minutes)
    return None  # Timeout — never auto-approve


def send_and_wait(
    req: ApprovalRequest,
    timeout_minutes: int = 60,
    ntfy_url: Optional[str] = None,
    topic: Optional[str] = None,
    server_url: Optional[str] = None,
    token: Optional[str] = None,
) -> Optional[bool]:
    """
    Convenience wrapper: ensure approval server is running, send notification,
    then block until human responds or timeout.
    """
    port = int(os.environ.get("APPROVAL_SERVER_PORT", "8080"))
    start_approval_server(port=port)
    sent = send_approval_request(req, ntfy_url=ntfy_url, topic=topic, server_url=server_url, token=token)
    if not sent:
        log.error("ntfy.notification_not_sent_blocking_anyway", run_id=req.run_id)
    return wait_for_approval(req.run_id, timeout_minutes=timeout_minutes)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_approval(run_id: str, approved: bool) -> None:
    """Write the approval decision to a state file for the pipeline to poll."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{run_id}_approval.json"
    path.write_text(
        json.dumps({"run_id": run_id, "approved": approved}),
        encoding="utf-8",
    )

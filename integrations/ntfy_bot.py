"""
FI-PR-GENERATOR — Ntfy Notification (Telegram Alternative)
Free, no account, no phone number needed.
Install ntfy app: https://ntfy.sh
"""
from __future__ import annotations

import asyncio
import os
import time

import requests
import structlog

from memory.schemas import ApprovalDecision, ApprovalMessage

log = structlog.get_logger(__name__)

NTFY_BASE = "https://ntfy.sh"


def _risk_emoji(score: float) -> str:
    if score < 30:   return "🟢 Low"
    if score < 60:   return "🟡 Medium"
    return "🔴 High"


def _test_emoji(result: str) -> str:
    if result == "pass":  return "✅ Passed"
    if result == "skip":  return "⚠️ Skipped"
    return "❌ Failed"


class NtfyApprovalBot:
    """
    Approval via ntfy.sh push notifications.
    
    Setup:
    1. Install ntfy app on phone (Play Store / App Store)
    2. Subscribe to your topic: e.g. "fi-pr-mera-2026"
    3. Set NTFY_TOPIC=fi-pr-mera-2026 in .env
    
    How approval works:
    - System sends notification to your phone
    - You open the link in notification → GitHub draft PR page
    - You reply YES/NO by sending a message to a special ntfy reply topic
    - System polls for your reply (simple HTTP GET)
    """

    def __init__(self):
        self._topic   = os.environ.get("NTFY_TOPIC", "")
        self._timeout = int(os.environ.get("APPROVAL_TIMEOUT_MINUTES", "60"))

        if not self._topic:
            raise EnvironmentError(
                "NTFY_TOPIC not set in .env\n"
                "1. Install ntfy app\n"
                "2. Choose a unique topic name\n"
                "3. Add NTFY_TOPIC=your-topic to .env"
            )

        self._reply_topic = f"{self._topic}-reply"

    def send_notification(self, message: str, title: str = "FI-PR-GENERATOR",
                          priority: str = "high", tags: list = None) -> bool:
        """Send a simple notification."""
        try:
            resp = requests.post(
                f"{NTFY_BASE}/{self._topic}",
                data=message.encode("utf-8"),
                headers={
                    "Title":    title,
                    "Priority": priority,
                    "Tags":     ",".join(tags or ["robot"]),
                },
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            log.warning("ntfy.send_error", error=str(e))
            return False

    def request_approval_sync(
        self, msg: ApprovalMessage
    ) -> tuple[ApprovalDecision, str]:
        """
        Send approval request and poll for reply.
        
        User replies by publishing to {topic}-reply:
          - "yes" or "approve" → APPROVED
          - "no" or "reject"   → REJECTED
          - "revise"           → REVISE
        
        Easy way to reply:
          curl -d "yes" ntfy.sh/{topic}-reply
        Or use the ntfy app's publish feature.
        """
        # Format the notification message
        files_str = ", ".join(msg.changed_files[:5])
        if len(msg.changed_files) > 5:
            files_str += f" +{len(msg.changed_files)-5} more"

        notification_text = (
            f"PR Ready for Review!\n"
            f"Issue: {msg.title}\n"
            f"Files: {files_str}\n"
            f"Tests: {_test_emoji(msg.status)}\n"
            f"Risk: {_risk_emoji(msg.risk_score)} ({msg.risk_score:.0f}/100)\n"
            f"Link: {msg.issue_link}\n\n"
            f"Reply YES to approve, NO to reject:\n"
            f"Open ntfy app → topic '{self._reply_topic}' → publish message"
        )

        # Send notification
        self.send_notification(
            message  = notification_text,
            title    = f"Approve PR: {msg.title[:40]}",
            priority = "urgent" if msg.risk_score > 60 else "high",
            tags     = ["warning" if msg.risk_score > 60 else "white_check_mark"],
        )
        log.info("ntfy.approval_sent", topic=self._topic, title=msg.title)
        print(f"\n📱 Notification sent to ntfy topic: {self._topic}")
        print(f"   Reply by publishing to: {self._reply_topic}")
        print(f"   Quick approve: curl -d 'yes' ntfy.sh/{self._reply_topic}")
        print(f"   Quick reject:  curl -d 'no'  ntfy.sh/{self._reply_topic}")

        # Clear any old replies first
        seen_ids = self._get_stale_reply_ids()

        # Poll for reply
        return self._poll_for_reply(seen_ids)

    def _get_stale_reply_ids(self) -> set[str]:
        """Fetch all message IDs from the last 10 minutes to ignore them as stale."""
        seen_ids = set()
        try:
            resp = requests.get(
                f"{NTFY_BASE}/{self._reply_topic}/json",
                params={"poll": "1", "since": "10m"},
                timeout=5,
            )
            if resp.status_code == 200 and resp.text.strip():
                import json as _json
                for line in resp.text.strip().splitlines():
                    try:
                        event = _json.loads(line)
                        msg_id = event.get("id")
                        if msg_id:
                            seen_ids.add(msg_id)
                    except Exception:
                        continue
        except Exception as e:
            log.warning("ntfy.drain_error", error=str(e))
        return seen_ids

    def _poll_for_reply(self, seen_ids: set[str]) -> tuple[ApprovalDecision, str]:
        """Poll the reply topic until we get a valid response or timeout."""
        deadline = time.time() + (self._timeout * 60)
        poll_interval = 5   # seconds between polls

        print(f"\n⏳ Waiting for your approval (timeout: {self._timeout} min)...", flush=True)

        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{NTFY_BASE}/{self._reply_topic}/json",
                    params={"poll": "1", "since": "10m"},
                    timeout=10,
                )
                if resp.status_code == 200 and resp.text.strip():
                    import json as _json
                    for line in resp.text.strip().splitlines():
                        try:
                            event = _json.loads(line)
                            msg_id = event.get("id")
                            if msg_id and msg_id not in seen_ids:
                                seen_ids.add(msg_id)
                                if event.get("event") == "message":
                                    message = event.get("message", "").strip().lower()
                                    decision = self._parse_reply(message)
                                    if decision:
                                        print(f"\n✅ Approval received: {message.upper()}", flush=True)
                                        log.info("ntfy.reply_received",
                                                 message=message, decision=decision.value)
                                        return decision, ""
                        except Exception:
                            continue

            except Exception as e:
                log.debug("ntfy.poll_error", error=str(e))

            remaining = int((deadline - time.time()) / 60)
            print(f"\r⏳ Waiting... {remaining}min remaining. "
                  f"Reply: curl -d 'yes' ntfy.sh/{self._reply_topic}  ", end="", flush=True)
            time.sleep(poll_interval)

        print("\n⏰ Approval timed out")
        log.warning("ntfy.timeout", timeout_minutes=self._timeout)
        return ApprovalDecision.TIMEOUT, ""

    def _parse_reply(self, message: str) -> ApprovalDecision | None:
        """Parse user reply into a decision."""
        message = message.lower().strip()
        if message in {"yes", "y", "approve", "approved", "ok", "go", "1"}:
            return ApprovalDecision.APPROVED
        if message in {"no", "n", "reject", "rejected", "stop", "0"}:
            return ApprovalDecision.REJECTED
        if message in {"revise", "r", "edit", "change", "redo"}:
            return ApprovalDecision.REVISE
        return None

    async def request_approval(
        self, msg: ApprovalMessage
    ) -> tuple[ApprovalDecision, str]:
        """Async wrapper for orchestrator compatibility."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self.request_approval_sync, msg
        )

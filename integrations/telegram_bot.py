"""
FI-PR-GENERATOR — Telegram Approval Bot
Sends diff + risk score to Telegram. Waits for inline button response.
Approve / Reject / Revise — non-blocking with timeout.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Optional

import structlog

from memory.schemas import ApprovalDecision, ApprovalMessage

log = structlog.get_logger(__name__)

# Callback data constants
CB_APPROVE = "approve"
CB_REJECT  = "reject"
CB_REVISE  = "revise"


def _format_risk_emoji(risk_score: float) -> str:
    if risk_score < 30:
        return "🟢 Low"
    elif risk_score < 60:
        return "🟡 Medium"
    return "🔴 High"


def _format_test_emoji(test_summary: str) -> str:
    low = test_summary.lower()
    if "pass" in low and "fail" not in low:
        return "✅ Passed"
    if "skip" in low:
        return "⚠️ Skipped (no test found)"
    if "env" in low or "environment" in low:
        return "⚠️ Env issue (not code bug)"
    return "❌ Failed"


def build_telegram_message(msg: ApprovalMessage) -> str:
    """
    Build a mobile-friendly Telegram message for human approval.
    Short, scannable, actionable.
    """
    files_list = "\n".join(f"  • `{f}`" for f in msg.changed_files[:8])
    if len(msg.changed_files) > 8:
        files_list += f"\n  ... +{len(msg.changed_files) - 8} more"

    # Trim diff preview to ~20 lines
    diff_lines = msg.diff_preview.splitlines()[:20]
    diff_preview = "\n".join(diff_lines)
    if len(msg.diff_preview.splitlines()) > 20:
        diff_preview += "\n... (truncated)"

    text = f"""🤖 *FI-PR-GENERATOR — Approval Request*

📋 *{msg.title}*

🔗 Issue: {msg.issue_link}
🌿 Branch: `{msg.branch}`

📁 *Changed Files ({len(msg.changed_files)}):*
{files_list}

🧪 *Tests:* {_format_test_emoji(msg.test_summary)}
{msg.test_summary[:200] if msg.test_summary else "_No test output_"}

⚠️ *Risk:* {_format_risk_emoji(msg.risk_score)} ({msg.risk_score:.0f}/100)

```diff
{diff_preview}
```

🕐 {msg.timestamp[:16]} UTC
"""
    return text


class TelegramApprovalBot:
    """
    Non-blocking Telegram bot that:
    1. Sends an approval message with inline buttons
    2. Waits for callback response (with timeout)
    3. Returns ApprovalDecision enum
    """

    def __init__(self):
        self._token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self._timeout = int(os.environ.get("APPROVAL_TIMEOUT_MINUTES", "60"))

        if not self._token or not self._chat_id:
            raise EnvironmentError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env"
            )

    async def request_approval(
        self, msg: ApprovalMessage
    ) -> tuple[ApprovalDecision, str]:
        """
        Send approval message and wait for human response.
        Returns (decision, rejection_reason).
        Timeout returns (ApprovalDecision.TIMEOUT, "").
        """
        try:
            from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
            from telegram.ext import Application, CallbackQueryHandler
        except ImportError:
            log.error("telegram.not_installed")
            raise ImportError("Run: pip install python-telegram-bot")

        text     = build_telegram_message(msg)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve & Push", callback_data=CB_APPROVE),
                InlineKeyboardButton("❌ Reject",         callback_data=CB_REJECT),
            ],
            [
                InlineKeyboardButton("🔄 Request Revision", callback_data=CB_REVISE),
            ],
        ])

        bot = Bot(token=self._token)
        decision_future: asyncio.Future = asyncio.get_event_loop().create_future()
        rejection_reason = ""

        async def _callback_handler(update, context):
            nonlocal rejection_reason
            query = update.callback_query
            await query.answer()

            data = query.data
            if data == CB_APPROVE:
                decision_future.set_result(ApprovalDecision.APPROVED)
                await query.edit_message_text("✅ Approved! Pushing branch...")
            elif data == CB_REJECT:
                decision_future.set_result(ApprovalDecision.REJECTED)
                await query.edit_message_text("❌ Rejected. Logging to org memory.")
            elif data == CB_REVISE:
                decision_future.set_result(ApprovalDecision.REVISE)
                await query.edit_message_text("🔄 Marked for revision. Re-planning...")

        # Send message
        async with bot:
            await bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            log.info("telegram.approval_sent", chat_id=self._chat_id,
                     title=msg.title)

        # Wait for response with timeout
        app = Application.builder().token(self._token).build()
        app.add_handler(CallbackQueryHandler(_callback_handler))

        try:
            await asyncio.wait_for(
                self._run_app_until_response(app, decision_future),
                timeout=self._timeout * 60,
            )
            decision = decision_future.result()
            log.info("telegram.response_received", decision=decision.value)
        except asyncio.TimeoutError:
            decision = ApprovalDecision.TIMEOUT
            log.warning("telegram.approval_timeout",
                        timeout_minutes=self._timeout)
            async with bot:
                await bot.send_message(
                    chat_id=self._chat_id,
                    text=f"⏰ Approval timed out after {self._timeout}min. Task preserved.",
                )

        return decision, rejection_reason

    async def _run_app_until_response(
        self, app, future: asyncio.Future
    ) -> None:
        """Run the telegram app until a button is pressed."""
        async with app:
            await app.start()
            try:
                while not future.done():
                    await asyncio.sleep(1)
            finally:
                await app.stop()

    def send_notification(self, message: str) -> None:
        """Send a simple notification (no buttons). Fire-and-forget."""
        asyncio.run(self._send_text(message))

    async def _send_text(self, message: str) -> None:
        try:
            from telegram import Bot
            async with Bot(token=self._token) as bot:
                await bot.send_message(
                    chat_id=self._chat_id,
                    text=message[:4096],   # Telegram max length
                    parse_mode="Markdown",
                )
            log.info("telegram.notification_sent")
        except Exception as e:
            log.warning("telegram.notification_failed", error=str(e))


# ─── Convenience function ────────────────────────────────────

def compute_risk_score(
    diff_line_count: int,
    changed_files: list[str],
    test_result: str,
    retrieval_confidence: float,
) -> float:
    """
    Risk = 0.35 × DiffSize
         + 0.25 × FileCriticality
         + 0.20 × TestCoverageGap
         + 0.20 × ConfidenceLoss

    Returns 0–100.
    """
    # Diff size score (200 lines = max risk)
    diff_score = min(100, (diff_line_count / 200) * 100)

    # File criticality (config/auth/db files are high risk)
    HIGH_RISK_PATTERNS = {"auth", "security", "password", "token", "config",
                          "database", "db", "payment", "stripe", "secret"}
    critical = sum(1 for f in changed_files
                   if any(p in f.lower() for p in HIGH_RISK_PATTERNS))
    file_score = min(100, critical * 30)

    # Test coverage gap
    test_score = 0 if test_result == "pass" else 70

    # Confidence loss (low confidence = higher risk)
    confidence_score = (1 - retrieval_confidence) * 100

    risk = (0.35 * diff_score +
            0.25 * file_score +
            0.20 * test_score +
            0.20 * confidence_score)

    return round(min(100, risk), 1)

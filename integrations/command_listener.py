"""
integrations/command_listener.py — ntfy command bot for mobile-first operation.

Subscribes to a dedicated ntfy command topic via Server-Sent Events (SSE).
When the user sends "org/repo" from the ntfy app on their phone, it triggers
the pipeline automatically.

Mobile workflow:
  1. Open ntfy app → publish to command topic: "viru0909-dev/nyay-setu-working"
  2. Server receives it, starts pipeline automatically
  3. Approval notification arrives on phone
  4. Tap Approve/Reject
  5. Server waits for next command

Two ntfy topics:
  - NTFY_COMMAND_TOPIC  — user sends org/repo TO this (input)
  - NTFY_TOPIC          — system sends approvals FROM this (output)

Rate limiting: Max 3 runs/hour. Commands within 20 min of last run are rejected.
Dry-run default: All commands run in dry-run unless suffixed with "--live".

Usage:
    python main.py listen              # Start command bot (dry-run only)
    python main.py listen --live       # Allow live (non-dry-run) commands
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import structlog

log = structlog.get_logger(__name__)

# Rate limit: minimum seconds between pipeline runs
MIN_RUN_INTERVAL_SECONDS = 20 * 60  # 20 minutes
MAX_RUNS_PER_HOUR = 3


def _load_allowed_orgs() -> list[str]:
    """Load allowed org names from config/orgs.json."""
    config_path = Path("config/orgs.json")
    if not config_path.exists():
        return []
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        return [
            o.get("name")
            for o in config.get("orgs", [])
            if isinstance(o, dict) and o.get("name")
        ]
    except Exception:
        return []


def _send_status(topic: str, message: str, token: str = "") -> None:
    """Send a status message back to the command topic."""
    ntfy_url = os.environ.get("NTFY_URL", "https://ntfy.sh")
    payload = {
        "topic": topic,
        "message": message,
        "title": "FI-PR Pipeline Status",
        "priority": 3,
        "tags": ["gear"],
    }
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        requests.post(ntfy_url, json=payload, headers=headers, timeout=10)
    except Exception:
        pass  # Best effort — don't crash the listener


def _parse_command(message: str) -> tuple[Optional[str], Optional[str], bool]:
    """
    Parse a command message into (org, repo, live_flag).

    Accepted formats:
      "org/repo"              → dry-run
      "org/repo --live"       → live run
      "org/repo --dry-run"    → dry-run (explicit)

    Returns (None, None, False) if the message is not a valid command.
    """
    text = message.strip()
    if not text or "/" not in text:
        return None, None, False

    live = False
    if "--live" in text:
        text = text.replace("--live", "").strip()
        live = True
    if "--dry-run" in text:
        text = text.replace("--dry-run", "").strip()
        live = False

    parts = text.split("/", 1)
    if len(parts) != 2:
        return None, None, False

    org = parts[0].strip()
    repo = parts[1].strip()

    if not org or not repo:
        return None, None, False

    # Basic sanitation — only allow alphanumeric, dash, underscore, dot
    import re
    if not re.match(r"^[a-zA-Z0-9._-]+$", org) or not re.match(r"^[a-zA-Z0-9._-]+$", repo):
        return None, None, False

    return org, repo, live


def listen_for_commands(
    allow_live: bool = False,
    command_topic: Optional[str] = None,
    token: Optional[str] = None,
) -> None:
    """
    Subscribe to the ntfy command topic and run pipelines on demand.

    This function blocks forever (until Ctrl+C), listening for incoming
    messages on the command topic. Each valid "org/repo" message triggers
    a pipeline run.

    Parameters:
        allow_live      — If True, "--live" suffix in commands enables non-dry-run mode
        command_topic   — ntfy topic to listen on (default: NTFY_COMMAND_TOPIC env var)
        token           — ntfy auth token (default: NTFY_TOKEN env var)
    """
    ntfy_url = os.environ.get("NTFY_URL", "https://ntfy.sh")
    command_topic = command_topic or os.environ.get("NTFY_COMMAND_TOPIC", "")
    token = token or os.environ.get("NTFY_TOKEN", "")

    if not command_topic:
        log.error(
            "command_listener.no_topic",
            hint="Set NTFY_COMMAND_TOPIC in .env (e.g., fi-pr-commands-ahad-2k26)",
        )
        raise EnvironmentError(
            "NTFY_COMMAND_TOPIC not set. Add it to .env to enable the command listener."
        )

    allowed_orgs = _load_allowed_orgs()
    log.info(
        "command_listener.starting",
        topic=command_topic,
        allowed_orgs=allowed_orgs or "(any)",
        allow_live=allow_live,
    )

    # Track rate limiting
    run_timestamps: list[float] = []
    last_run_time: float = 0

    subscribe_url = f"{ntfy_url.rstrip('/')}/{command_topic}/json"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    while True:
        try:
            log.info("command_listener.connecting", url=subscribe_url)
            with requests.get(
                subscribe_url,
                stream=True,
                headers=headers,
                timeout=None,  # Long-poll / SSE — no timeout
            ) as resp:
                resp.raise_for_status()
                log.info("command_listener.connected", status=resp.status_code)

                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue

                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # ntfy sends different event types; we only care about "message"
                    if msg.get("event") != "message":
                        continue

                    text = msg.get("message", "").strip()
                    if not text:
                        continue

                    log.info("command_listener.received", message=text)

                    # Parse the command
                    org, repo, live_requested = _parse_command(text)
                    if not org or not repo:
                        log.warning("command_listener.invalid_command", raw=text)
                        _send_status(
                            command_topic,
                            f"❌ Invalid command: '{text}'\nUse format: org/repo",
                            token,
                        )
                        continue

                    # Validate org against whitelist
                    disable_whitelist = os.environ.get("DISABLE_ORG_WHITELIST", "").lower() == "true"
                    if not disable_whitelist and allowed_orgs and org not in allowed_orgs:
                        log.warning("command_listener.org_rejected", org=org, allowed=allowed_orgs)
                        _send_status(
                            command_topic,
                            f"❌ Org '{org}' not in whitelist.\n"
                            f"Allowed: {', '.join(allowed_orgs)}",
                            token,
                        )
                        continue

                    # Rate limiting
                    now = time.monotonic()
                    if now - last_run_time < MIN_RUN_INTERVAL_SECONDS:
                        remaining = int((MIN_RUN_INTERVAL_SECONDS - (now - last_run_time)) / 60)
                        log.warning("command_listener.rate_limited", wait_minutes=remaining)
                        _send_status(
                            command_topic,
                            f"⏳ Rate limited — wait {remaining} min before next run.",
                            token,
                        )
                        continue

                    # Clean old timestamps and check hourly limit
                    run_timestamps[:] = [t for t in run_timestamps if now - t < 3600]
                    if len(run_timestamps) >= MAX_RUNS_PER_HOUR:
                        log.warning("command_listener.hourly_limit", count=len(run_timestamps))
                        _send_status(
                            command_topic,
                            f"⏳ Hourly limit reached ({MAX_RUNS_PER_HOUR}/hr). Try again later.",
                            token,
                        )
                        continue

                    # Determine dry_run mode
                    dry_run = True
                    if live_requested and allow_live:
                        dry_run = False
                    elif live_requested and not allow_live:
                        log.warning("command_listener.live_not_allowed")
                        _send_status(
                            command_topic,
                            "⚠️ --live requested but listener started without --live flag. Running as dry-run.",
                            token,
                        )

                    mode = "DRY RUN" if dry_run else "LIVE"
                    _send_status(
                        command_topic,
                        f"🚀 Starting pipeline [{mode}]: {org}/{repo}",
                        token,
                    )

                    # Run the pipeline
                    try:
                        from orchestrator import run_pipeline

                        last_run_time = time.monotonic()
                        run_timestamps.append(last_run_time)

                        log.info(
                            "command_listener.running_pipeline",
                            org=org, repo=repo, dry_run=dry_run,
                        )

                        final_state = run_pipeline(
                            org=org, repo=repo, dry_run=dry_run,
                        )

                        # Send result summary
                        status_icon = "✅" if final_state.state == "completed" else "⚠️"
                        summary_lines = [
                            f"{status_icon} Pipeline {final_state.state.upper()}: {org}/{repo}",
                        ]
                        if final_state.issue_number:
                            summary_lines.append(f"Issue: #{final_state.issue_number}")
                        if final_state.model_used:
                            summary_lines.append(f"Model: {final_state.model_used}")
                        if final_state.risk_score:
                            summary_lines.append(
                                f"Risk: {final_state.risk_score.level} "
                                f"({final_state.risk_score.score:.0f}/100)"
                            )
                        if final_state.failure_reason:
                            summary_lines.append(f"Reason: {final_state.failure_reason[:100]}")

                        _send_status(command_topic, "\n".join(summary_lines), token)

                    except Exception as exc:
                        log.error("command_listener.pipeline_error", error=str(exc))
                        _send_status(
                            command_topic,
                            f"❌ Pipeline crashed: {str(exc)[:200]}",
                            token,
                        )

        except requests.ConnectionError:
            log.warning("command_listener.connection_lost", retry_seconds=30)
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("command_listener.stopped")
            break
        except Exception as exc:
            log.error("command_listener.error", error=str(exc))
            time.sleep(30)

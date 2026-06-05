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
MIN_RUN_INTERVAL_SECONDS = 1 * 60  # 20 minutes
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


def _get_repos_for_org(org: str) -> list[str]:
    """Get whitelisted repository names for a given organization."""
    config_path = Path("config/orgs.json")
    if not config_path.exists():
        return []
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for o in config.get("orgs", []):
            if isinstance(o, dict) and o.get("name", "").lower() == org.lower():
                return [r.get("name") if isinstance(r, dict) else r for r in o.get("repos", [])]
        return []
    except Exception:
        return []


def register_active_repos(targets: list[tuple[str, str]]) -> None:
    """
    Ensure the whitelisted targets are in config/orgs.json, mark them enabled: true,
    and disable (enabled: false) all other repositories.
    """
    config_path = Path("config/orgs.json")
    if not config_path.exists():
        data = {
            "orgs": [],
            "issue_score_threshold": 60,
            "max_diff_lines": 200,
            "max_retries": 2,
            "max_runs_per_hour": 3
        }
    else:
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return

    target_set = {(o.lower(), r.lower()) for o, r in targets}
    orgs_list = data.setdefault("orgs", [])

    for o_name, r_name in targets:
        # Find/create org
        org_entry = None
        for o in orgs_list:
            if isinstance(o, dict) and o.get("name", "").lower() == o_name.lower():
                org_entry = o
                break
        if not org_entry:
            org_entry = {"name": o_name, "repos": []}
            orgs_list.append(org_entry)

        # Find/create repo
        repos_list = org_entry.setdefault("repos", [])
        repo_entry = None
        for r in repos_list:
            r_val = r.get("name") if isinstance(r, dict) else r
            if r_val.lower() == r_name.lower():
                repo_entry = r
                break
        if not repo_entry:
            repos_list.append({"name": r_name, "enabled": True})

    # Enable targets, disable others
    for o in orgs_list:
        if not isinstance(o, dict):
            continue
        o_name = o.get("name", "")
        repos_list = o.setdefault("repos", [])
        new_repos = []
        for r in repos_list:
            if isinstance(r, dict):
                r_name = r.get("name", "")
                r["enabled"] = (o_name.lower(), r_name.lower()) in target_set
                new_repos.append(r)
            else:
                new_repos.append({"name": r, "enabled": (o_name.lower(), r.lower()) in target_set})
        o["repos"] = new_repos

    try:
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.info("orgs_config.updated_active_targets", targets=targets)
    except Exception as exc:
        log.warning("orgs_config.update_failed", error=str(exc))


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


def _parse_command(
    message: str
) -> tuple[list[tuple[str, str]], Optional[int], bool, str, Optional[str]]:
    """
    Parse a command message into (targets, issue_number, live_flag, mode_name, error_msg).

    Supported formats:
      1. Deep mode (single repo):
         "org/repo"
         "org/repo --live"
         "org/repo --issue 123"
      2. Selected mode (multiple repos in an org):
         "org/repo1,repo2"
         "org/repo1,repo2 --live"
      3. Org mode (entire org):
         "org"
         "org --live"
    """
    text = message.strip()
    if not text:
        return [], None, False, "unknown", "Empty command"

    # 1. Parse live/dry-run options
    live = False
    if "--live" in text:
        text = text.replace("--live", "").strip()
        live = True
    if "--dry-run" in text:
        text = text.replace("--dry-run", "").strip()
        live = False
    if "--dryrun" in text:
        text = text.replace("--dryrun", "").strip()
        live = False

    # 2. Parse issue option using regex
    import re
    issue_number = None
    issue_match = re.search(r"--issue\s+(\d+)", text)
    if issue_match:
        issue_number = int(issue_match.group(1))
        text = re.sub(r"--issue\s+\d+", "", text).strip()
    else:
        issue_match = re.search(r"--issue=(\d+)", text)
        if issue_match:
            issue_number = int(issue_match.group(1))
            text = re.sub(r"--issue=\d+", "", text).strip()
        else:
            issue_match = re.search(r"-i\s+(\d+)", text)
            if issue_match:
                issue_number = int(issue_match.group(1))
                text = re.sub(r"-i\s+\d+", "", text).strip()

    # 3. Determine if it's org mode or repo mode
    if "/" not in text:
        # Org mode
        org = text.strip()
        # Basic validation
        if not re.match(r"^[a-zA-Z0-9._-]+$", org):
            return [], None, False, "org", f"Invalid organization name format: '{org}'"

        repos = _get_repos_for_org(org)
        if not repos:
            return [], None, False, "org", f"No repositories configured in config/orgs.json for org '{org}'"

        targets = [(org, r) for r in repos]
        return targets, issue_number, live, "org", None

    # Repo mode (Selected or Deep)
    parts = text.split("/", 1)
    if len(parts) != 2:
        return [], None, False, "unknown", "Invalid command format. Use org/repo or org/repo1,repo2"

    org = parts[0].strip()
    repos_part = parts[1].strip()

    if not org or not repos_part:
        return [], None, False, "unknown", "Org or repository name cannot be empty"

    if not re.match(r"^[a-zA-Z0-9._-]+$", org):
        return [], None, False, "unknown", f"Invalid organization name format: '{org}'"

    # Split repos by comma or space
    repo_names = [r.strip() for r in re.split(r"[\s,]+", repos_part) if r.strip()]
    if not repo_names:
        return [], None, False, "unknown", "No repository names found in command"

    # Validate repo formats
    for r in repo_names:
        if not re.match(r"^[a-zA-Z0-9._-]+$", r):
            return [], None, False, "unknown", f"Invalid repository name format: '{r}'"

    targets = [(org, r) for r in repo_names]
    mode = "deep" if len(targets) == 1 else "selected"
    return targets, issue_number, live, mode, None


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

                    # LOOP PREVENTION: Ignore status updates sent by the bot itself
                    title = msg.get("title", "")
                    tags = msg.get("tags", []) or []
                    if title == "FI-PR Pipeline Status" or "gear" in tags:
                        continue

                    text = msg.get("message", "").strip()
                    if not text:
                        continue

                    if any(text.startswith(p) for p in [
                        "🚀 Starting pipeline",
                        "⏳ Rate limited",
                        "❌ Invalid command",
                        "❌ Pipeline",
                        "⚠️ Pipeline",
                        "✅ Pipeline",
                        "⏳ Hourly limit",
                    ]):
                        continue

                    log.info("command_listener.received", message=text)

                    # Reload environment variables dynamically to pick up any changes in .env
                    from dotenv import load_dotenv
                    load_dotenv(override=True)

                    # Parse the command
                    targets, issue_number, live_requested, mode_name, error_msg = _parse_command(text)
                    if error_msg or not targets:
                        log.warning("command_listener.invalid_command", raw=text, error=error_msg)
                        _send_status(
                            command_topic,
                            f"❌ Invalid command: '{text}'\nError: {error_msg or 'No targets found'}\nUse formats: org, org/repo, or org/repo1,repo2",
                            token,
                        )
                        continue

                    # Validate org against whitelist
                    disable_whitelist = os.environ.get("DISABLE_ORG_WHITELIST", "").lower() == "true"
                    if not disable_whitelist and allowed_orgs:
                        rejected_orgs = [o for o, r in targets if o not in allowed_orgs]
                        if rejected_orgs:
                            log.warning("command_listener.org_rejected", rejected=rejected_orgs, allowed=allowed_orgs)
                            _send_status(
                                command_topic,
                                f"❌ Org whitelist violation.\n"
                                f"Orgs not whitelisted: {', '.join(set(rejected_orgs))}\n"
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

                    mode_desc = f"{mode_name.upper()} mode"
                    targets_desc = ", ".join(f"{o}/{r}" for o, r in targets)
                    run_mode = "DRY RUN" if dry_run else "LIVE"

                    _send_status(
                        command_topic,
                        f"🚀 Starting pipeline ({mode_desc} - [{run_mode}]): {targets_desc}",
                        token,
                    )

                    # Auto-register and whitelist/enable all targets of this run, disabling all others
                    register_active_repos(targets)

                    # Run the pipeline sequentially for all targets
                    for org, repo in targets:
                        try:
                            from orchestrator import run_pipeline

                            last_run_time = time.monotonic()
                            run_timestamps.append(last_run_time)

                            log.info(
                                "command_listener.running_pipeline",
                                org=org, repo=repo, dry_run=dry_run,
                            )

                            final_state = run_pipeline(
                                org=org, repo=repo, dry_run=dry_run, issue_number=issue_number
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
                            log.error("command_listener.pipeline_error", org=org, repo=repo, error=str(exc))
                            _send_status(
                                command_topic,
                                f"❌ Pipeline crashed on {org}/{repo}: {str(exc)[:200]}",
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

        except requests.ConnectionError:
            log.warning("command_listener.connection_lost", retry_seconds=30)
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("command_listener.stopped")
            break
        except Exception as exc:
            log.error("command_listener.error", error=str(exc))
            time.sleep(30)

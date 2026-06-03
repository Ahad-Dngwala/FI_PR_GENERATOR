"""
FI-PR-GENERATOR — Code Generation Agent
Calls Aider with Claude (primary) → Qwen → DeepSeek fallback chain.
Enforces scope guard: auto-rejects diffs > MAX_DIFF_LINES.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import structlog

from integrations.aider_runner import run_aider_fix
from memory.schemas import OrgMemory
from memory.org_memory import get_memory_summary

log = structlog.get_logger(__name__)

MAX_DIFF_LINES = int(os.environ.get("MAX_DIFF_LINES", "200"))
MAX_RETRIES    = 2


def _load_model_chain() -> list[dict]:
    """Load coding model chain from config."""
    config_path = Path(__file__).parent.parent / "config" / "models.json"
    if config_path.exists():
        data = json.loads(config_path.read_text())
        return data.get("coding_chain", [])
    # Fallback defaults
    return [
        {"id": "claude-primary",  "model": "claude-sonnet-4-5"},
        {"id": "qwen-fallback",   "model": "qwen/qwen-2.5-coder-72b-instruct"},
        {"id": "deepseek-fallback","model": "deepseek/deepseek-coder-v2"},
    ]


class CoderAgent:
    """
    Code generation agent with:
    - Fallback chain (Claude → Qwen → DeepSeek)
    - Scope guard (auto-reject if diff > MAX_DIFF_LINES)
    - Retry with error feedback
    """

    def __init__(self):
        self._chain     = _load_model_chain()
        self._dry_run   = os.environ.get("DRY_RUN", "false").lower() == "true"

    def generate_patch(
        self,
        local_path: Path,
        issue_title: str,
        issue_body: str,
        issue_number: int,
        approved_files: list[str],
        memory: Optional[OrgMemory] = None,
        prior_error: str = "",
        git_ops=None,
    ) -> tuple[bool, str, str]:
        """
        Generate a code patch for the issue.

        Returns:
            (success: bool, model_used: str, failure_reason: str)

        Side effects:
            - Aider edits files in local_path
            - If scope guard fails, changes are NOT committed
        """
        issue_text = self._build_issue_text(
            issue_title, issue_body, issue_number,
            memory, prior_error,
        )

        extra_context = ""
        if memory:
            extra_context = get_memory_summary(memory)

        # Try each model in the chain
        for model_cfg in self._chain:
            model_id = model_cfg["model"]
            log.info("coder.attempting", model=model_id,
                     files=approved_files, issue=issue_number)

            success, output = run_aider_fix(
                local_path   = local_path,
                issue_text   = issue_text,
                approved_files=approved_files,
                model        = model_id,
                dry_run      = self._dry_run,
                extra_context= extra_context,
            )

            if not success:
                log.warning("coder.model_failed", model=model_id,
                            output=output[:200])
                continue   # try next model

            # ── Scope guard ────────────────────────────────────
            if git_ops:
                diff_lines = git_ops.get_diff_line_count()
                log.info("coder.scope_check",
                         diff_lines=diff_lines, max=MAX_DIFF_LINES)

                if diff_lines > MAX_DIFF_LINES:
                    log.warning("coder.scope_exceeded",
                                diff_lines=diff_lines, max=MAX_DIFF_LINES,
                                model=model_id)
                    # Reset the changes — scope too large
                    try:
                        git_ops.repo.git.checkout("--", ".")
                    except Exception:
                        pass
                    return (False, model_id,
                            f"Diff too large: {diff_lines} lines (max {MAX_DIFF_LINES}). "
                            f"Re-plan with narrower scope.")

            log.info("coder.success", model=model_id)
            return True, model_id, ""

        # All models exhausted
        return False, "", "All coding models failed or rate-limited"

    def _build_issue_text(
        self,
        title: str,
        body: str,
        number: int,
        memory: Optional[OrgMemory],
        prior_error: str,
    ) -> str:
        """Build the issue description sent to Aider."""
        lines = [
            f"GitHub Issue #{number}: {title}",
            "",
            body or "(no body)",
            "",
        ]

        if prior_error:
            lines += [
                "PREVIOUS ATTEMPT FAILED — fix the following error:",
                prior_error[:500],
                "",
            ]

        if memory and memory.accepted_pr_examples:
            ex = memory.accepted_pr_examples[-1]
            lines += [
                f"Example of an accepted PR in this repo: '{ex.get('pr_title', '')}'",
                f"Files it changed: {', '.join(ex.get('files_changed', [])[:3])}",
                "",
            ]

        return "\n".join(lines)

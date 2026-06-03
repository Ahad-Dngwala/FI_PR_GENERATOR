"""
FI-PR-GENERATOR — Memory Builder Agent
Uses Gemini Flash to extract org conventions from merged PR history.
Run this once per org, then refresh weekly with incremental updates.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Optional

import structlog

from memory.schemas import OrgMemory
from memory.org_memory import save_memory, load_memory

log = structlog.get_logger(__name__)


class MemoryBuilder:
    """
    Extracts structured repository knowledge from:
    - Merged PR history (titles, bodies, labels, file patterns)
    - Maintainer review comments
    - Common file hotspots
    """

    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            log.warning("memory_builder.no_gemini_key",
                        msg="Memory build will use rule-based extraction only")
            self._client = None
        else:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            self._client = genai.GenerativeModel("gemini-2.0-flash")

    def build_from_prs(
        self,
        org: str,
        repo: str,
        prs: list[dict],
        default_branch: str = "main",
        activity_score: float = 70.0,
        require_assignment: bool = True,
    ) -> OrgMemory:
        """
        Build or update org memory from a list of merged PRs.
        Returns the updated OrgMemory (also saves to disk).
        """
        log.info("memory_builder.building",
                 org=org, repo=repo, pr_count=len(prs))

        # Load existing memory or create fresh
        memory = load_memory(org, repo) or OrgMemory(
            org_name=org,
            repo_name=repo,
            default_branch=default_branch,
        )
        memory.activity_score    = activity_score
        memory.require_assignment= require_assignment

        if not prs:
            log.warning("memory_builder.no_prs", org=org, repo=repo)
            save_memory(memory)
            return memory

        # ── Rule-based extraction (fast, no LLM) ─────────────
        memory = self._extract_by_rules(memory, prs)

        # ── LLM extraction (richer patterns) ─────────────────
        if self._client:
            memory = self._extract_by_llm(memory, prs)

        save_memory(memory)
        log.info("memory_builder.complete",
                 org=org, repo=repo, version=memory.version)
        return memory

    def _extract_by_rules(self, memory: OrgMemory, prs: list[dict]) -> OrgMemory:
        """
        Rule-based extraction of commit style, hotspots, test commands.
        Fast, deterministic, no API cost.
        """
        commit_prefixes = {}
        all_files: list[str] = []
        all_labels: list[str] = []

        for pr in prs:
            # Commit message style analysis
            title = pr.get("title", "")
            for pattern, style in [
                (r"^fix(\(.*?\))?:", "fix(scope): description"),
                (r"^feat(\(.*?\))?:", "feat(scope): description"),
                (r"^docs?:", "docs: description"),
                (r"^fix:", "fix: description"),
                (r"^\[fix\]", "[fix] description"),
            ]:
                if re.match(pattern, title, re.I):
                    commit_prefixes[style] = commit_prefixes.get(style, 0) + 1
                    break

            # File hotspots
            all_files.extend(pr.get("files_changed", []))

            # Labels
            all_labels.extend(pr.get("labels", []))

        # Most common commit style
        if commit_prefixes:
            memory.commit_style = max(commit_prefixes, key=commit_prefixes.get)

        # Top file hotspots
        from collections import Counter
        file_counts = Counter(all_files)
        memory.common_file_hotspots = [f for f, _ in file_counts.most_common(10)]

        # Common labels
        label_counts = Counter(all_labels)
        memory.common_issue_labels = [lb for lb, _ in label_counts.most_common(8)]

        # PR title style
        if memory.commit_style:
            memory.pr_title_style = memory.commit_style

        # Acceptance patterns from labels
        GOOD_LABELS = {"documentation", "bug", "good first issue",
                       "fix", "enhancement", "feature"}
        for lb, count in label_counts.items():
            if lb.lower() in GOOD_LABELS and lb not in memory.issue_acceptance_patterns:
                memory.issue_acceptance_patterns.append(lb.lower())

        log.debug("memory_builder.rules_done",
                  hotspots=memory.common_file_hotspots[:5],
                  commit_style=memory.commit_style)
        return memory

    def _extract_by_llm(self, memory: OrgMemory, prs: list[dict]) -> OrgMemory:
        """
        Use Gemini to extract richer patterns:
        - Review turnaround time
        - Test commands from PR bodies
        - Maintainer preferences
        - Rejection patterns
        """
        # Send top 10 most recent PRs to Gemini
        sample = prs[:10]
        pr_text = "\n\n---\n\n".join([
            f"PR #{p['number']}: {p['title']}\n"
            f"Labels: {', '.join(p.get('labels', []))}\n"
            f"Files: {', '.join(p.get('files_changed', [])[:5])}\n"
            f"Body excerpt: {p.get('body', '')[:300]}"
            for p in sample
        ])

        prompt = f"""Analyze these merged pull requests from {memory.org_name}/{memory.repo_name}.

{pr_text}

Extract structured information and respond with ONLY valid JSON:
{{
  "commit_style": "<detected commit message format or empty>",
  "pr_title_style": "<detected PR title format or empty>",
  "common_test_commands": ["<cmd1>", ...],
  "common_lint_commands": ["<cmd1>", ...],
  "maintainer_preferences": ["<preference>", ...],
  "acceptance_patterns": ["<pattern>", ...],
  "rejection_patterns": ["<pattern>", ...],
  "review_turnaround_days": <number or 7>
}}

Only include information you can confidently infer. Use empty arrays if unsure."""

        try:
            resp = self._client.generate_content(prompt)
            text = resp.text.strip()
            # Strip markdown code blocks if present
            text = re.sub(r"^```(?:json)?\n?", "", text)
            text = re.sub(r"\n?```$", "", text)

            data = json.loads(text)

            if data.get("commit_style") and not memory.commit_style:
                memory.commit_style = data["commit_style"]
            if data.get("pr_title_style") and not memory.pr_title_style:
                memory.pr_title_style = data["pr_title_style"]
            if data.get("common_test_commands"):
                memory.common_test_commands = data["common_test_commands"][:3]
            if data.get("common_lint_commands"):
                memory.common_lint_commands = data["common_lint_commands"][:3]
            if data.get("acceptance_patterns"):
                for p in data["acceptance_patterns"]:
                    if p not in memory.issue_acceptance_patterns:
                        memory.issue_acceptance_patterns.append(p)
            if data.get("rejection_patterns"):
                for p in data["rejection_patterns"]:
                    if p not in memory.issue_rejection_patterns:
                        memory.issue_rejection_patterns.append(p)
            if data.get("review_turnaround_days"):
                memory.review_turnaround_days = float(data["review_turnaround_days"])

            log.info("memory_builder.llm_done",
                     test_commands=memory.common_test_commands)

        except Exception as e:
            log.warning("memory_builder.llm_error", error=str(e))

        return memory

    def incremental_refresh(
        self,
        org: str,
        repo: str,
        new_prs: list[dict],
        activity_score: float = 70.0,
    ) -> OrgMemory:
        """
        Update existing memory with only new PRs (delta refresh).
        Much cheaper than full rebuild. Run nightly.
        """
        memory = load_memory(org, repo)
        if not memory:
            return self.build_from_prs(org, repo, new_prs,
                                       activity_score=activity_score)

        if not new_prs:
            log.info("memory_builder.no_new_prs", org=org, repo=repo)
            memory.last_refresh = datetime.utcnow().isoformat()
            memory.activity_score = activity_score
            save_memory(memory)
            return memory

        log.info("memory_builder.incremental_refresh",
                 org=org, repo=repo, new_pr_count=len(new_prs))
        memory = self._extract_by_rules(memory, new_prs)
        if self._client:
            memory = self._extract_by_llm(memory, new_prs)

        memory.activity_score = activity_score
        save_memory(memory)
        return memory

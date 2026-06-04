# ═══════════════════════════════════════════════════════════════════════════════
# FI-PR-GENERATOR — Complete Prompt Inventory
# ═══════════════════════════════════════════════════════════════════════════════
#
# This file documents EVERY prompt sent to EVERY LLM in the system.
# Use this file when debugging why a model is producing bad output — the
# problem is usually in the prompt, not the model or the code.
#
# Format per entry:
#   MODULE       — source file
#   LLM          — which model receives this prompt
#   ROLE         — what job this prompt does in the pipeline
#   TRIGGER      — when it is called
#   PROMPT TEXT  — exact prompt (with {placeholders} for dynamic values)
#   OUTPUT FORMAT — what the system expects back
#   FALLBACK     — what happens if this prompt fails
#   TUNING TIPS  — how to improve this prompt if results are poor
# ═══════════════════════════════════════════════════════════════════════════════

---

## 1. Issue Clarity Scoring

**MODULE:** `agents/scorer.py` → `_score_clarity()`
**LLM:** Llama 3.1 8B Instant (Groq)
**ROLE:** Score how clearly a GitHub issue is written (0–100). High clarity issues
are more likely to be fixable without back-and-forth with the maintainer.
**TRIGGER:** Called once per issue during issue selection step.

```
Score the clarity of this GitHub issue from 0 to 100.
High score means: has clear reproduction steps, describes expected vs actual behavior,
has defined acceptance criteria, is not vague or ambiguous.
Low score means: one-liner, no context, vague request.

Issue text:
{issue_title + "\n" + issue_body[:1500]}

Respond with only a number between 0 and 100.
```

**OUTPUT FORMAT:** A single integer or decimal (e.g. `75` or `72.5`).
**FALLBACK:** `_heuristic_clarity()` — keyword-based scoring (30 + 17.5 per signal found).
**TUNING TIPS:**
- If scores are too low overall → add examples of "high clarity" issues to the prompt
- If all issues score the same → increase context window (currently truncated at 1500 chars)
- If model returns text instead of number → add "Only respond with a digit."

---

## 2. Test Failure Classification

**MODULE:** `integrations/test_runner.py` → `_classify_with_llm()`
**LLM:** Llama 3.1 8B Instant (Groq)
**ROLE:** Classify a test failure as CODE_BUG / ENV_ISSUE / FLAKY / PREEXISTING / UNRELATED.
Only called when rule-based matching is inconclusive.
**TRIGGER:** After every test run that fails, if no pattern matched with certainty.

```
Classify this test failure output as exactly one of:
CODE_BUG, ENV_ISSUE, FLAKY, PREEXISTING, UNRELATED

Definitions:
CODE_BUG    = the patch introduced a bug (assertion, type, syntax error)
ENV_ISSUE   = missing env var, missing dependency, service not running
FLAKY       = likely intermittent, non-deterministic failure
PREEXISTING = failure unrelated to the changes (was failing before)
UNRELATED   = test file unrelated to changed files

Test output:
{last 1500 chars of test output}

Respond with ONLY the class name. No explanation.
```

**OUTPUT FORMAT:** Exactly one of: `CODE_BUG` `ENV_ISSUE` `FLAKY` `PREEXISTING` `UNRELATED`
**FALLBACK:** Defaults to `"CODE_BUG"` if LLM unavailable or response is not a known class.
**TUNING TIPS:**
- If FLAKY is never returned → add examples of flaky output (timing errors, port conflicts)
- If ENV_ISSUE is confused with CODE_BUG → add more examples under ENV_ISSUE definition
- If model always returns same class → increase temperature slightly (0.1 → 0.3)

---

## 3. Memory Builder — Conventions Extraction

**MODULE:** `agents/memory_builder.py` → `_extract_conventions()`
**LLM:** Gemini 2.0 Flash (Google AI Studio) → fallback: Llama 70B (Groq)
**ROLE:** Analyze merged PR history to extract repo conventions: commit style, branch
naming, test commands, hotspot files, accepted/rejected issue types.
**TRIGGER:** Called once during `build-memory` command or first pipeline run on a new repo.

```
Analyze these merged pull requests from {org}/{repo} and extract repository conventions.

Merged PRs:
{compact PR summaries: title, branch, labels, additions/deletions, review notes}

Extract and return as JSON with exactly these keys:
{
  "conventions": {
    "commit_style": "example commit message style",
    "branch_naming": "pattern like fix/description-123",
    "test_commands": ["list", "of", "test", "commands"],
    "build_command": "build command if detectable",
    "package_manager": "npm/pip/cargo/etc",
    "primary_language": "main language"
  },
  "file_knowledge": {
    "hotspots": ["top 10 most-changed files"],
    "test_directory": "path to test directory or empty string",
    "config_files": ["list of config files changed"]
  },
  "pattern_learning": {
    "accepted_issue_types": ["types of issues that get merged: docs/bug/frontend/etc"],
    "rejected_issue_types": ["types commonly rejected or not merged"],
    "accepted_pr_size_avg_lines": 0,
    "maintainer_preferences": ["up to 5 specific preferences from review comments"]
  }
}

Respond with valid JSON only.
```

**OUTPUT FORMAT:** JSON with keys: `conventions`, `file_knowledge`, `pattern_learning`
**FALLBACK:** Returns empty dict `{}` — system uses defaults everywhere.
**TUNING TIPS:**
- If commit_style is wrong → ask for "the most common prefix pattern like 'fix:' or 'feat:'"
- If maintainer_preferences is empty → add "look specifically at review rejection comments"
- If model invents conventions → add "only report what you see in the data, not what you assume"

---

## 4. Contribution Workflow Detector

**MODULE:** `agents/memory_builder.py` → `_detect_workflow()`
**LLM:** Llama 3.1 70B Versatile (Groq) → fallback: Gemini 2.0 Flash
**ROLE:** Dynamically discover HOW this repo expects contributions to happen.
Does it use a claim bot? Must you propose first? Can you send a direct PR?
**TRIGGER:** Called during `build-memory` and during incremental refresh.

```
You are analyzing the contribution workflow for {org}/{repo}.

Study ALL the evidence below and discover how contributors are expected to submit changes.
Do NOT assume standard GitHub flow — every repo is different.

Evidence:
=== CONTRIBUTING.md ===
{contributing_md[:3000]}

=== ISSUE TEMPLATES ===
{issue_templates[:3 templates, each truncated to 500 chars]}

=== BOT/MAINTAINER COMMENTS ===
{up to 20 bot/maintainer comments, each truncated to 150 chars}

=== RECENT MERGED PR TITLES/BRANCHES ===
{last 20 merged PR titles and branch names}

Your task: Identify contribution workflow patterns from the evidence. Be specific.
For each pattern you detect, describe it clearly. If you see something unusual or novel,
capture it in raw_patterns even if it does not fit standard categories.

Return ONLY valid JSON with this structure:
{
  "assignment_required": true or false or null,
  "claim_bot_present": true or false or null,
  "claim_command": "/claim or /assign or !take or null",
  "proposal_required": true or false or null,
  "direct_pr_allowed": true or false or null,
  "pr_template_required": true or false or null,
  "issue_template_required": true or false or null,
  "bot_assigns": true or false or null,
  "maintainer_assigns": true or false or null,
  "self_assign_allowed": true or false or null,
  "confidence": 0.0 to 1.0 based on evidence quality,
  "inferred_from": ["list of source documents that provided evidence"],
  "raw_patterns": [
    {
      "pattern_name": "short descriptive name",
      "description": "what this pattern means for contributors",
      "evidence": "specific text or observation that revealed this pattern",
      "affects": "what stage of contribution this affects: issue/pr/review/merge"
    }
  ]
}

Use null for fields where you have no evidence. Be honest about confidence.
Respond with JSON only.
```

**OUTPUT FORMAT:** JSON matching `WorkflowRules` schema.
**FALLBACK:** Returns `WorkflowRules(confidence=0.1)` — system uses standard GitHub flow assumptions.
**TUNING TIPS:**
- If claim_command is missed → add "Look for any text like '/claim', '/assign', '!take', 'claimed by'"
- If confidence is always low → provide more bot comment examples in the evidence
- If raw_patterns is empty → add "Even if workflow looks standard, describe at least 1 pattern you observed"

---

## 5. Code Generation (Primary + Fallbacks)

**MODULE:** `agents/coder.py` → `generate_patch()` + `prompts/coder.txt`
**LLM:**
  1. Gemini 2.5 Pro (Google AI Studio) — PRIMARY
  2. Qwen 2.5 Coder 72B (OpenRouter) — FALLBACK 1
  3. DeepSeek Coder V2 (OpenRouter) — FALLBACK 2
  4. Claude Sonnet (Anthropic) — FALLBACK 3 (paid)
**ROLE:** Generate a minimal unified diff (patch) that fixes the GitHub issue.
**TRIGGER:** Called in Step 5 of the pipeline, up to MAX_RETRIES+1 times.

```
You are a coding assistant working on a specific open-source GitHub issue.

CONTEXT:
Repository: {repo_name}
Issue #{issue_number}: {issue_title}
Issue description: {issue_body[:3000]}
Files to modify: {relevant_files}
Commit style in this repo: {commit_style}
Example merged commit: {example_commit}
Maintainer preferences: {maintainer_preferences}
Contribution workflow: {workflow_summary}

STRICT RULES:
1. Only modify files listed above — no other files
2. Match existing code style exactly
3. Keep changes minimal — fix only what the issue describes
4. Add or update tests if a test directory exists
5. Do not add new dependencies without checking package.json or requirements.txt
6. Do not refactor unrelated code

OUTPUT FORMAT:
Respond with only a unified diff (--- a/file / +++ b/file format).
No explanations. No markdown. No preamble.
Start output immediately with the diff.
```

On retry (after test failure or scope exceeded), this is appended:
```
--- PREVIOUS ATTEMPT FAILED ---
Previous attempt failed with the following error. Fix it:
{error_context[-1000:]}
```

**OUTPUT FORMAT:** Pure unified diff starting with `--- a/` or `+++ b/`.
**FALLBACK:** Tries next model in chain. If ALL fail → `AllModelsExhaustedError`.
**TUNING TIPS:**
- If diff is too large → strengthen STRICT RULES with "Maximum 50 lines changed"
- If model adds markdown → change OUTPUT FORMAT to "The VERY FIRST character of your response must be `-`"
- If model modifies wrong files → add the full content of each relevant file to context
- If model adds new imports → add "Do NOT add any new imports"

---

## 6. Independent Code Review

**MODULE:** `agents/reviewer.py` → `review_patch()`
**LLM:** Qwen 2.5 Coder 32B Preview (Groq) — intentionally different from coder
**ROLE:** Review the generated patch for bugs, style issues, missing tests, and
unrelated changes. Classify issues as critical (pipeline retries) or minor (human sees notes).
**TRIGGER:** Called in Step 6, after patch is applied and tests run.

```
Review this git patch for a GitHub issue. Be a strict but fair code reviewer.

Issue #{issue_number}: {issue_title}
Issue description: {issue_body[:500]}

Maintainer preferences for this repository:
{maintainer_preferences[:5]}

Patch to review:
{diff[:8000]}

Check ALL of the following:
1. Does the patch actually solve the stated issue?
2. Are there missing imports, undefined variables, or syntax errors?
3. Are edge cases handled (null/empty inputs, boundary conditions)?
4. Does the style match the maintainer preferences listed above?
5. Are there any files modified that are UNRELATED to the issue?
6. Is new behavior added without corresponding tests?

Classify each problem as:
- "critical": would definitely cause test failure or maintainer rejection
- "minor": style issue or suggestion, but patch is still acceptable

Respond ONLY as valid JSON:
{
  "approved": true or false,
  "critical_issues": ["list of critical problems, empty if none"],
  "minor_issues": ["list of minor problems or suggestions, empty if none"],
  "summary": "one-sentence review summary"
}

approved = false only if there are critical issues.
approved = true even if there are minor issues (human will see them)
```

**OUTPUT FORMAT:** JSON with `approved`, `critical_issues`, `minor_issues`, `summary`.
**FALLBACK:** Returns `(True, ["Reviewer unavailable — inspect diff manually"])`.
**TUNING TIPS:**
- If critical/minor distinction is wrong → give concrete examples of each
- If reviewer always approves → add "Be strict. If in doubt, mark as critical."
- If reviewer flags too many things → add "Only flag things the MAINTAINER would likely reject"
- If summary is vague → add "Summary must mention the specific function or file changed"

---

## Summary Table

| # | Prompt | LLM | Provider | Called By | Output |
|---|--------|-----|----------|-----------|--------|
| 1 | Issue Clarity | Configurable (Default: `llama-3.1-8b-instant`) | Groq | scorer.py | Number 0-100 |
| 2 | Test Classification | Configurable (Default: `llama-3.1-8b-instant`) | Groq | test_runner.py | One class name |
| 3 | Conventions Extraction | Configurable (Default: `gemini-2.0-flash`) | Google AI | memory_builder.py | JSON dict |
| 4 | Workflow Detection | Configurable (Default: `llama-3.3-70b-versatile`) | Groq | memory_builder.py | JSON WorkflowRules |
| 5 | Code Generation | Configurable (`coding_chain` fallback list) | Multiple | coder.py | Unified diff |
| 6 | Code Review | Configurable (Default: `llama-3.3-70b-versatile`) | Groq | reviewer.py | JSON verdict |

## Debugging Checklist

If a model is producing bad output:

1. **Check the raw prompt** — add a `print(prompt)` before the API call to see exactly what the model receives
2. **Check input size** — is the issue body or diff being truncated at a bad point?
3. **Check JSON mode** — Groq and Gemini have `response_format={"type": "json_object"}` — verify it's enabled
4. **Test the prompt manually** — paste it into the model's web interface and see what you get
5. **Check the fallback** — if the primary model fails silently, is the fallback running?
6. **Inspect logs** — run with `LOG_LEVEL=DEBUG python main.py ...` for full structured logs
7. **Unit test the prompt** — write a test in `tests/test_llm_endpoints.py` with a specific input you want to debug

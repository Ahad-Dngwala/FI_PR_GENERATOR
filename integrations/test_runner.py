"""
integrations/test_runner.py — Test detection, execution, and failure classification.

Rule-based pattern matching runs first (free, instant).
LLM classification (Llama 3.1 8B on Groq) is used only when rules are inconclusive.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional
from memory.config_loader import get_model_name, get_model_provider

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Failure pattern definitions (rule-based, deterministic)
# ---------------------------------------------------------------------------

FAILURE_PATTERNS: dict[str, list[str]] = {
    "CODE_BUG": [
        "AssertionError",
        "TypeError",
        "NameError",
        "AttributeError",
        "SyntaxError",
        "IndentationError",
        "FAILED",
        "FAIL",
        "✗ FAIL",
        "test.*FAILED",
        "Error:",
    ],
    "ENV_ISSUE": [
        "ModuleNotFoundError",
        "ImportError",
        "ENOENT",
        "ECONNREFUSED",
        "SECRET",
        "password",
        "Connection refused",
        "Cannot find module",
        "command not found",
        "No such file or directory",
        "ENOMEM",
        "port already in use",
        "getaddrinfo",
    ],
}

# Classification outcomes
OUTCOMES = ("CODE_BUG", "ENV_ISSUE", "FLAKY", "PREEXISTING", "UNRELATED", "PASS")


def detect_test_command(repo_path: str) -> Optional[str]:
    """
    Auto-detect the project's test command by inspecting known config files.

    Search order:
    1. package.json → scripts.test
    2. pytest.ini or setup.cfg → [tool:pytest]
    3. Makefile → test target
    4. .github/workflows/*.yml → step with 'test' in name/run
    5. pyproject.toml → [tool.pytest.ini_options]

    Returns None if no test command is found.
    """
    root = Path(repo_path)

    # 1. package.json
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if "test" in scripts:
                log.info("test_runner.detected", source="package.json", command="npm test")
                return "npm test"
            if "lint" in scripts:
                return "npm run lint"
        except (json.JSONDecodeError, OSError):
            pass

    # 2. pytest.ini / setup.cfg / pyproject.toml
    for conf in ["pytest.ini", "setup.cfg", "pyproject.toml"]:
        if (root / conf).exists():
            log.info("test_runner.detected", source=conf, command="python -m pytest")
            return "python -m pytest --tb=short -q"

    # 3. Makefile with test target
    makefile = root / "Makefile"
    if makefile.exists():
        content = makefile.read_text(encoding="utf-8", errors="replace")
        if "test:" in content or "test :" in content:
            log.info("test_runner.detected", source="Makefile", command="make test")
            return "make test"

    # 4. GitHub Actions workflow
    workflow_dir = root / ".github" / "workflows"
    if workflow_dir.exists():
        for yml in workflow_dir.glob("*.yml"):
            content = yml.read_text(encoding="utf-8", errors="replace")
            # Look for a test step
            if "pytest" in content:
                log.info("test_runner.detected", source=str(yml.name), command="python -m pytest")
                return "python -m pytest --tb=short -q"
            if "npm test" in content or "npm run test" in content:
                log.info("test_runner.detected", source=str(yml.name), command="npm test")
                return "npm test"

    log.warning("test_runner.no_test_command_found", repo=repo_path)
    return None


def run_tests(
    repo_path: str,
    test_command: Optional[str],
    timeout_seconds: int = 300,
) -> tuple[bool, str]:
    """
    Execute test_command in a subprocess with a timeout.

    Returns:
        (passed: bool, output: str)

    passed=True if returncode == 0, False otherwise.
    If test_command is None, returns (True, "NO_TEST_COMMAND") — treated as ENV_ISSUE.
    """
    if not test_command:
        log.warning("test_runner.no_command")
        return True, "NO_TEST_COMMAND"

    log.info("test_runner.running", command=test_command, repo=repo_path)
    try:
        result = subprocess.run(
            test_command,
            shell=True,  # Needed for compound commands like "npm test"
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=timeout_seconds,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        passed = result.returncode == 0
        log.info(
            "test_runner.done",
            passed=passed,
            returncode=result.returncode,
            output_lines=len(output.splitlines()),
        )
        return passed, output
    except subprocess.TimeoutExpired:
        log.warning("test_runner.timeout", timeout=timeout_seconds)
        return False, f"TIMEOUT after {timeout_seconds}s"
    except Exception as exc:
        log.error("test_runner.exception", error=str(exc))
        return False, f"RUNNER_ERROR: {exc}"


def classify_failure(output: str, repo_path: str = "") -> str:
    """
    Classify a test failure output into one of:
    CODE_BUG, ENV_ISSUE, FLAKY, PREEXISTING, UNRELATED, PASS

    Step 1: Deterministic rule-based matching (free, instant).
    Step 2: If ambiguous, send to Llama 3.1 8B on Groq.
    """
    if not output or output == "NO_TEST_COMMAND":
        return "ENV_ISSUE"

    output_lower = output.lower()

    # Check for clean pass keywords first
    pass_signals = ["passed", "ok", "0 failed", "all tests passed", "✓"]
    if any(s in output_lower for s in pass_signals):
        # Only PASS if no failure patterns override
        if not any(
            p.lower() in output_lower
            for patterns in FAILURE_PATTERNS.values()
            for p in patterns
        ):
            return "PASS"

    # Rule-based classification
    matched: dict[str, int] = {}
    for category, patterns in FAILURE_PATTERNS.items():
        hits = sum(1 for p in patterns if p.lower() in output_lower)
        if hits:
            matched[category] = hits

    if matched:
        # Return the category with most pattern hits
        winner = max(matched, key=lambda k: matched[k])
        log.info("test_runner.classified_rule", category=winner, hits=matched)
        return winner

    # Ambiguous — use Groq Llama 3.1 8B
    log.info("test_runner.llm_classification_needed")
    return _classify_with_llm(output)


def _classify_with_llm(output: str) -> str:
    """
    Send truncated test output to Llama 3.1 8B on Groq for classification.
    Returns one of the known outcome strings.
    """
    provider = get_model_provider("classifier_provider", "groq")
    model = get_model_name("classifier_model", "llama-3.1-8b-instant")
    truncated = output[-1500:]  # last 1500 chars

    # Only check GROQ_API_KEY if we are actually using Groq
    api_key = os.environ.get("GROQ_API_KEY")
    if provider == "groq" and not api_key:
        log.warning("test_runner.no_groq_key_for_classifier", fallback="CODE_BUG")
        return "CODE_BUG"

    prompt = (
        "Classify this test failure output as exactly one of:\n"
        "CODE_BUG, ENV_ISSUE, FLAKY, PREEXISTING, UNRELATED\n\n"
        "Definitions:\n"
        "CODE_BUG = the patch introduced a bug (assertion, type, syntax error)\n"
        "ENV_ISSUE = missing env var, missing dependency, service not running\n"
        "FLAKY = likely intermittent, non-deterministic failure\n"
        "PREEXISTING = failure unrelated to the changes (was failing before)\n"
        "UNRELATED = test file unrelated to changed files\n\n"
        f"Test output:\n{truncated}\n\n"
        "Respond with ONLY the class name. No explanation."
    )

    try:
        result = None
        # Try Ollama classification if configured
        if provider == "ollama":
            try:
                from integrations.ollama_client import call_ollama
                result = call_ollama(
                    model=model,
                    prompt=prompt,
                    temperature=0.0,
                    max_tokens=10,
                ).strip().upper()
                if result in OUTCOMES:
                    log.info("test_runner.classified_ollama", category=result)
                    return result
            except Exception as exc:
                log.warning("test_runner.classifier_ollama_failed", error=str(exc)[:100], fallback="groq")

        # Try Groq classification (fallback or primary)
        if not result:
            if provider == "groq" or (provider != "ollama" and api_key):
                from groq import Groq
                client = Groq(api_key=api_key)
                response = client.chat.completions.create(
                    model=model if provider == "groq" else "llama-3.1-8b-instant",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=10,
                    temperature=0.0,
                )
                result = response.choices[0].message.content.strip().upper()

        if result in OUTCOMES:
            log.info("test_runner.classified_llm", category=result)
            return result
        log.warning("test_runner.llm_invalid_response", raw=result, fallback="CODE_BUG")
        return "CODE_BUG"
    except Exception as exc:
        log.error("test_runner.llm_classify_failed", error=str(exc), fallback="CODE_BUG")
        return "CODE_BUG"


def run_preflight(repo_path: str) -> dict:
    """
    Run tests on the unmodified main branch before any coding.

    Records which tests fail as PREEXISTING so they don't block the pipeline later.

    Returns:
        {
            "test_command": str | None,
            "preexisting_failures": list[str],
            "passed": bool,
        }
    """
    test_command = detect_test_command(repo_path)
    if not test_command:
        return {"test_command": None, "preexisting_failures": [], "passed": True}

    log.info("test_runner.preflight_start", repo=repo_path)
    passed, output = run_tests(repo_path, test_command, timeout_seconds=300)

    # Extract failing test names from output (rough heuristic)
    preexisting: list[str] = []
    for line in output.splitlines():
        line_stripped = line.strip()
        if "FAILED" in line_stripped or "FAIL" in line_stripped:
            # Try to extract a test identifier
            if "::" in line_stripped:
                # pytest format: path/test_file.py::TestClass::test_name
                preexisting.append(line_stripped.split()[-1] if line_stripped.split() else line_stripped)

    log.info(
        "test_runner.preflight_done",
        passed=passed,
        preexisting_count=len(preexisting),
    )
    return {
        "test_command": test_command,
        "preexisting_failures": preexisting,
        "passed": passed,
    }

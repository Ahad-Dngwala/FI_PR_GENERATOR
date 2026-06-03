"""
FI-PR-GENERATOR — Test Runner + Failure Classifier
Runs repo tests and classifies failures using rules-first, LLM-fallback.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import structlog

from memory.schemas import FailureClass, ValidationResult

log = structlog.get_logger(__name__)

# Max test runtime (seconds)
TEST_TIMEOUT = 180

# ─────────────────────────────────────────────────────────────
# Test command detection
# ─────────────────────────────────────────────────────────────

COMMAND_SOURCES = [
    # (file, parse_fn) — checked in order
    ("package.json",        "_parse_npm_test"),
    ("pyproject.toml",      "_parse_pyproject_test"),
    ("Makefile",            "_parse_makefile_test"),
    (".github/workflows",   "_parse_ci_workflow"),
    ("pytest.ini",          "_parse_pytest_ini"),
    ("setup.cfg",           "_parse_setup_cfg"),
]

FALLBACK_COMMANDS = [
    ["python", "-m", "pytest", "--tb=short", "-q"],
    ["npm", "test", "--", "--watchAll=false"],
    ["yarn", "test", "--watchAll=false"],
    ["pnpm", "test"],
    ["make", "test"],
]


def detect_test_command(local_path: Path) -> Optional[list[str]]:
    """
    Detect the correct test command for the repo.
    Returns a list of args (e.g. ["npm", "test"]) or None.
    """
    path = Path(local_path)

    # package.json
    pkg = path / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            scripts = data.get("scripts", {})
            if "test" in scripts:
                log.info("test_runner.detected", source="package.json",
                         cmd=scripts["test"])
                return ["npm", "test", "--", "--watchAll=false", "--passWithNoTests"]
        except Exception:
            pass

    # pyproject.toml
    if (path / "pyproject.toml").exists():
        content = (path / "pyproject.toml").read_text()
        if "[tool.pytest" in content or "pytest" in content:
            return ["python", "-m", "pytest", "--tb=short", "-q", "--no-header"]

    # pytest.ini / setup.cfg
    if (path / "pytest.ini").exists() or (path / "setup.cfg").exists():
        return ["python", "-m", "pytest", "--tb=short", "-q", "--no-header"]

    # Makefile with test target
    makefile = path / "Makefile"
    if makefile.exists():
        content = makefile.read_text()
        if re.search(r"^test:", content, re.MULTILINE):
            return ["make", "test"]

    # Try fallback commands (check if binary exists)
    for cmd in FALLBACK_COMMANDS:
        if _binary_exists(cmd[0]):
            log.info("test_runner.fallback_detected", cmd=cmd)
            return cmd

    log.warning("test_runner.no_command_found")
    return None


def _binary_exists(name: str) -> bool:
    return subprocess.run(["where" if os.name == "nt" else "which", name],
                          capture_output=True).returncode == 0


# ─────────────────────────────────────────────────────────────
# Rule-based failure classifier (fast, free, reliable)
# ─────────────────────────────────────────────────────────────

# Order matters — more specific patterns first
FAILURE_PATTERNS: list[tuple[re.Pattern, FailureClass]] = [
    # Environment issues
    (re.compile(r"ModuleNotFoundError|ImportError|Cannot find module|"
                r"command not found|No such file or directory|"
                r"ENOENT|env: .* No such", re.I), FailureClass.ENV_ISSUE),
    (re.compile(r"EACCES|Permission denied|Access is denied", re.I), FailureClass.ENV_ISSUE),
    (re.compile(r"missing.*secret|SECRET.*missing|API_KEY.*undefined|"
                r"GITHUB_TOKEN.*not set", re.I), FailureClass.ENV_ISSUE),
    (re.compile(r"Connection refused|ECONNREFUSED|connection timeout|"
                r"network error", re.I), FailureClass.ENV_ISSUE),

    # Preexisting failures
    (re.compile(r"(This test (was|is) (?:already )?failing|"
                r"pre-existing|known failure|skip)", re.I), FailureClass.PREEXISTING),

    # Flaky indicators
    (re.compile(r"flaky|intermittent|timing|timeout.*retry|"
                r"sleep.*assertion|race condition", re.I), FailureClass.FLAKY),
    (re.compile(r"ETIMEDOUT|socket hang up|jest.*timeout", re.I), FailureClass.FLAKY),

    # Code bugs — actual assertion / logic failures
    (re.compile(r"AssertionError|assert.*failed|Expected.*Received|"
                r"TypeError|AttributeError|NameError|KeyError|"
                r"SyntaxError|IndentationError|ReferenceError|"
                r"test.*FAILED|FAIL.*test", re.I), FailureClass.CODE_BUG),
]


def classify_failure_by_rules(stdout: str, stderr: str) -> FailureClass:
    """
    Rule-based failure classification.
    Fast, deterministic, no LLM call needed.
    """
    combined = f"{stdout}\n{stderr}"
    for pattern, failure_class in FAILURE_PATTERNS:
        if pattern.search(combined):
            log.debug("test_runner.classified_by_rule",
                      failure_class=failure_class.value)
            return failure_class
    return FailureClass.UNKNOWN


def classify_failure_with_llm(stdout: str, stderr: str,
                               groq_client) -> FailureClass:
    """
    LLM fallback classification when rules don't match.
    Uses Llama via Groq (fast + free).
    """
    try:
        snippet = f"STDOUT:\n{stdout[-800:]}\nSTDERR:\n{stderr[-800:]}"
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "user",
                "content": (
                    "Classify this test failure into exactly one category:\n"
                    "CODE_BUG | ENV_ISSUE | FLAKY | PREEXISTING | UNRELATED\n\n"
                    f"{snippet}\n\n"
                    "Reply with ONLY the category name, nothing else."
                ),
            }],
            max_tokens=20,
            temperature=0.0,
        )
        text = response.choices[0].message.content.strip().upper()
        try:
            return FailureClass(text.lower())
        except ValueError:
            return FailureClass.UNKNOWN
    except Exception as e:
        log.warning("test_runner.llm_classify_error", error=str(e))
        return FailureClass.UNKNOWN


# ─────────────────────────────────────────────────────────────
# Main test runner
# ─────────────────────────────────────────────────────────────

def run_tests(local_path: Path, command: Optional[list[str]],
              retry_count: int = 0,
              groq_client=None,
              branch: str = "",
              repo: str = "") -> ValidationResult:
    """
    Run tests and return a classified ValidationResult.

    Strategy:
    1. Detect command if not provided
    2. Run with timeout
    3. Classify failure by rules first, LLM fallback
    4. Return structured result
    """
    if command is None:
        command = detect_test_command(local_path)

    if command is None:
        return ValidationResult(
            command="[none detected]",
            exit_code=-1,
            result="skip",
            failure_class=FailureClass.ENV_ISSUE,
            environment_notes="No test command found. Cannot validate locally.",
            branch=branch,
            repo=repo,
        )

    cmd_str = " ".join(command)
    log.info("test_runner.running", command=cmd_str, path=str(local_path))

    try:
        result = subprocess.run(
            command,
            cwd=str(local_path),
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT,
            env={**os.environ},
            shell=os.name == "nt",
        )
        exit_code = result.returncode
        stdout    = result.stdout
        stderr    = result.stderr

    except subprocess.TimeoutExpired:
        log.warning("test_runner.timeout", command=cmd_str)
        return ValidationResult(
            command=cmd_str,
            exit_code=124,
            stdout_summary="",
            stderr_summary="Test suite timed out (180s limit)",
            failure_class=FailureClass.ENV_ISSUE,
            result="fail",
            environment_notes="Test suite exceeded time limit.",
            retry_count=retry_count,
            branch=branch,
            repo=repo,
        )

    # Truncate for storage
    stdout_summary = _summarize(stdout, max_lines=30)
    stderr_summary = _summarize(stderr, max_lines=20)

    if exit_code == 0:
        log.info("test_runner.passed", command=cmd_str)
        return ValidationResult(
            command=cmd_str,
            exit_code=0,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            failure_class=FailureClass.UNKNOWN,
            result="pass",
            retry_count=retry_count,
            branch=branch,
            repo=repo,
        )

    # Classify the failure
    failure_class = classify_failure_by_rules(stdout, stderr)
    if failure_class == FailureClass.UNKNOWN and groq_client:
        failure_class = classify_failure_with_llm(stdout, stderr, groq_client)

    log.warning("test_runner.failed",
                command=cmd_str,
                exit_code=exit_code,
                failure_class=failure_class.value)

    return ValidationResult(
        command=cmd_str,
        exit_code=exit_code,
        stdout_summary=stdout_summary,
        stderr_summary=stderr_summary,
        failure_class=failure_class,
        result="fail",
        retry_count=retry_count,
        branch=branch,
        repo=repo,
    )


def _summarize(text: str, max_lines: int = 30) -> str:
    """Keep only last N lines of output (most relevant part)."""
    lines = text.strip().splitlines()
    if len(lines) <= max_lines:
        return text.strip()
    return "...\n" + "\n".join(lines[-max_lines:])


def should_retry(result: ValidationResult, max_retries: int = 2) -> bool:
    """
    Decide if we should retry.
    Only CODE_BUG and FLAKY failures get retries.
    """
    if result.result == "pass":
        return False
    if result.retry_count >= max_retries:
        return False
    return result.failure_class in (FailureClass.CODE_BUG, FailureClass.FLAKY)


def should_continue_despite_failure(result: ValidationResult) -> bool:
    """
    Return True if we can continue to human review despite the failure.
    Preexisting and env failures can be documented and sent to human.
    """
    return result.failure_class in (FailureClass.PREEXISTING,
                                    FailureClass.ENV_ISSUE,
                                    FailureClass.UNKNOWN)

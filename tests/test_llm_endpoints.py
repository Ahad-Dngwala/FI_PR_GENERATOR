"""
tests/test_llm_endpoints.py — Live smoke tests for all LLM API endpoints.

These tests make REAL API calls. They are skipped automatically if the
corresponding API key is not set in the environment.

Run: python -m pytest tests/test_llm_endpoints.py -v -s
  -s flag shows print output so you can see which models respond.

Each test makes the smallest possible call (1-5 tokens) to verify:
  - The API key is valid and accepted
  - The model exists and responds
  - The response format is as expected
"""

from __future__ import annotations

import os
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skip_if_no_key(env_var: str, service: str) -> None:
    """Skip the test with a clear message if the API key is not set."""
    if not os.environ.get(env_var):
        pytest.skip(f"{service} key not set ({env_var}). Add it to .env to run this test.")


def _make_minimal_chat_call(client_fn, model: str, prompt: str = "Say: OK") -> str:
    """Make a 1-token chat call and return the response text."""
    # client_fn returns (response_text, None) or raises
    return client_fn(model, prompt)


# ---------------------------------------------------------------------------
# Groq — Llama 3.1 8B (scoring/classification), Llama 70B (planning/workflow), Qwen 32B (review)
# ---------------------------------------------------------------------------


class TestGroqEndpoints:
    """
    Groq hosts 3 models used by this system:
      - llama-3.1-8b-instant      → issue clarity scoring, test failure classification
      - llama-3.1-70b-versatile   → workflow detection, memory conventions
      - qwen-2.5-coder-32b-preview → independent code review
    """

    @pytest.fixture(autouse=True)
    def check_key(self):
        _skip_if_no_key("GROQ_API_KEY", "Groq")

    def _call(self, model: str, prompt: str = "Respond with only: OK") -> str:
        from groq import Groq
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0.0,
        )
        return resp.choices[0].message.content.strip()

    def test_llama_8b_instant(self):
        """Llama 3.1 8B — used for: issue clarity scoring, test failure classification."""
        result = self._call("llama-3.1-8b-instant")
        assert result, "Expected non-empty response from llama-3.1-8b-instant"
        print(f"\n  llama-3.1-8b-instant responded: {result!r}")

    def test_llama_70b_versatile(self):
        """Llama 3.1 70B — used for: workflow detection, memory conventions extraction."""
        result = self._call("llama-3.1-70b-versatile")
        assert result, "Expected non-empty response from llama-3.1-70b-versatile"
        print(f"\n  llama-3.1-70b-versatile responded: {result!r}")

    def test_qwen_32b_coder_review(self):
        """Qwen 2.5 Coder 32B — used for: independent PR review."""
        result = self._call("qwen-2.5-coder-32b-preview")
        assert result, "Expected non-empty response from qwen-2.5-coder-32b-preview"
        print(f"\n  qwen-2.5-coder-32b-preview responded: {result!r}")

    def test_groq_json_mode(self):
        """Verify Groq JSON mode works (used in memory_builder and reviewer)."""
        from groq import Groq
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": 'Return: {"status": "ok"}'}],
            max_tokens=20,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        import json
        data = json.loads(resp.choices[0].message.content)
        assert "status" in data or len(data) > 0, "JSON mode returned empty object"
        print(f"\n  Groq JSON mode: {data}")


# ---------------------------------------------------------------------------
# Gemini — 2.5 Pro (primary coder), 2.0 Flash (memory builder)
# ---------------------------------------------------------------------------


class TestGeminiEndpoints:
    """
    Gemini models used:
      - gemini-2.5-pro   → primary code generation
      - gemini-2.0-flash → org memory conventions extraction (1M context)
    """

    @pytest.fixture(autouse=True)
    def check_key(self):
        _skip_if_no_key("GEMINI_API_KEY", "Gemini / Google AI Studio")

    def _call(self, model: str, prompt: str = "Respond with only: OK") -> str:
        import google.generativeai as genai
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        gmodel = genai.GenerativeModel(model)
        resp = gmodel.generate_content(prompt)
        return resp.text.strip()

    def test_gemini_2_5_pro(self):
        """Gemini 2.5 Pro — PRIMARY coder model."""
        result = self._call("gemini-2.5-pro")
        assert result, "Expected non-empty response from gemini-2.5-pro"
        print(f"\n  gemini-2.5-pro responded: {result[:80]!r}")

    def test_gemini_2_0_flash(self):
        """Gemini 2.0 Flash — memory builder, conventions extraction."""
        result = self._call("gemini-2.0-flash")
        assert result, "Expected non-empty response from gemini-2.0-flash"
        print(f"\n  gemini-2.0-flash responded: {result[:80]!r}")

    def test_gemini_json_mode(self):
        """Verify Gemini JSON response mode (used in memory_builder)."""
        import google.generativeai as genai
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        gmodel = genai.GenerativeModel("gemini-2.0-flash")
        resp = gmodel.generate_content(
            'Return a JSON object with key "status" set to "ok".',
            generation_config={"response_mime_type": "application/json"},
        )
        import json
        data = json.loads(resp.text)
        assert "status" in data or len(data) > 0
        print(f"\n  Gemini JSON mode: {data}")


# ---------------------------------------------------------------------------
# OpenRouter — Qwen 72B (fallback 1), DeepSeek Coder V2 (fallback 2)
# ---------------------------------------------------------------------------


class TestOpenRouterEndpoints:
    """
    OpenRouter fallback chain:
      - qwen/qwen-2.5-coder-72b-instruct → fallback coder 1
      - deepseek/deepseek-coder-v2       → fallback coder 2
    """

    @pytest.fixture(autouse=True)
    def check_key(self):
        _skip_if_no_key("OPENROUTER_API_KEY", "OpenRouter")

    def _call(self, model: str, prompt: str = "Respond with only: OK") -> str:
        from openai import OpenAI
        client = OpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.0,
        )
        return resp.choices[0].message.content.strip()

    def test_qwen_72b_coder(self):
        """Qwen 2.5 Coder 72B — fallback coder 1."""
        result = self._call("qwen/qwen-2.5-coder-72b-instruct")
        assert result, "Expected non-empty response"
        print(f"\n  qwen-2.5-coder-72b-instruct responded: {result[:80]!r}")

    def test_deepseek_coder_v2(self):
        """DeepSeek Coder V2 — fallback coder 2."""
        result = self._call("deepseek/deepseek-coder")
        assert result, "Expected non-empty response"
        print(f"\n  deepseek-coder responded: {result[:80]!r}")


# ---------------------------------------------------------------------------
# Anthropic — Claude Sonnet (last-resort fallback 3, paid)
# ---------------------------------------------------------------------------


class TestAnthropicEndpoints:
    """
    Anthropic Claude — last-resort fallback coder (paid).
    Only tested if ANTHROPIC_API_KEY is set.
    """

    @pytest.fixture(autouse=True)
    def check_key(self):
        _skip_if_no_key("ANTHROPIC_API_KEY", "Anthropic (paid — last resort fallback)")

    def test_claude_sonnet(self):
        """Claude Sonnet — last-resort fallback coder 3."""
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=10,
            messages=[{"role": "user", "content": "Respond with only: OK"}],
        )
        result = resp.content[0].text.strip()
        assert result, "Expected non-empty response from Claude Sonnet"
        print(f"\n  claude-sonnet-4-20250514 responded: {result!r}")


# ---------------------------------------------------------------------------
# GitHub API — token validity check
# ---------------------------------------------------------------------------


class TestGitHubEndpoint:
    """Verify GitHub token is valid and rate limit is not exhausted."""

    @pytest.fixture(autouse=True)
    def check_key(self):
        _skip_if_no_key("GITHUB_TOKEN", "GitHub")

    def test_github_token_valid(self):
        """Check token auth and rate limit status."""
        from github import Github
        gh = Github(os.environ["GITHUB_TOKEN"])
        user = gh.get_user()
        rate = gh.get_rate_limit()
        print(f"\n  GitHub token valid for: {user.login}")
        print(f"  Core rate limit: {rate.core.remaining}/{rate.core.limit}")
        assert user.login, "Expected valid GitHub user"
        assert rate.core.remaining > 0, (
            "GitHub rate limit exhausted! Wait until reset: "
            + str(rate.core.reset)
        )

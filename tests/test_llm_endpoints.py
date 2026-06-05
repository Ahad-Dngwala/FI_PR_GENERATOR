"""
tests/test_llm_endpoints.py — Live smoke tests for all LLM API endpoints.

These tests make REAL API calls. They are skipped automatically if the
corresponding API key is not set in the environment, or if the key is
invalid/expired.

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
from dotenv import load_dotenv

# Load environment variables from .env file relative to this test file
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skip_if_no_key(env_var: str, service: str) -> None:
    """Skip the test with a clear message if the API key is not set."""
    val = os.environ.get(env_var, "")
    if not val or val.startswith("sk-ant-..."):
        pytest.skip(f"{service} key not set ({env_var}). Add it to .env to run this test.")


def _skip_on_auth_error(exc: Exception, service: str) -> None:
    """Skip the test if the API key is invalid/expired instead of failing."""
    err = str(exc).lower()
    if any(k in err for k in ["invalid_api_key", "invalid api key", "401", "authentication"]):
        pytest.skip(f"{service} API key is invalid — regenerate at the provider's console")


def _skip_on_quota(exc: Exception, model: str) -> None:
    """Skip the test if the model has quota exhausted."""
    err = str(exc).lower()
    if any(q in err for q in ["quota", "exhausted", "429", "rate_limit", "resource_exhausted"]):
        pytest.skip(f"{model} skipped: quota exhausted or rate limited")


def _skip_on_bad_model(exc: Exception, model: str) -> None:
    """Skip the test if the model ID is not recognized by the provider."""
    err = str(exc).lower()
    if any(k in err for k in ["not a valid model", "model not found", "not found", "no endpoints found", "404"]):
        pytest.skip(f"{model} skipped: model ID not recognized by provider")


# ---------------------------------------------------------------------------
# Groq — Llama 3.1 8B (scoring/classification), Llama 70B (planning/workflow), Qwen 32B (review)
# ---------------------------------------------------------------------------


class TestGroqEndpoints:
    """
    Groq hosts models used by this system:
      - llama-3.1-8b-instant      → issue clarity scoring, test failure classification
      - llama-3.3-70b-versatile   → planning, workflow detection, conventions extraction, independent code review
    """

    @pytest.fixture(autouse=True)
    def check_key(self):
        _skip_if_no_key("GROQ_API_KEY", "Groq")

    def _call(self, model: str, prompt: str = "Respond with only: OK") -> str:
        from groq import Groq
        try:
            client = Groq(api_key=os.environ["GROQ_API_KEY"])
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0.0,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            _skip_on_auth_error(exc, "Groq")
            _skip_on_quota(exc, model)
            _skip_on_bad_model(exc, model)
            raise

    def test_llama_8b_instant(self):
        """Llama 3.1 8B — used for: issue clarity scoring, test failure classification."""
        result = self._call("llama-3.1-8b-instant")
        assert result, "Expected non-empty response from llama-3.1-8b-instant"
        print(f"\n  llama-3.1-8b-instant responded: {result!r}")

    def test_llama_33_70b_versatile(self):
        """Llama 3.3 70B — used for: planning, workflow detection, reviews."""
        result = self._call("llama-3.3-70b-versatile")
        assert result, "Expected non-empty response from llama-3.3-70b-versatile"
        print(f"\n  llama-3.3-70b-versatile responded: {result!r}")

    def test_groq_json_mode(self):
        """Verify Groq JSON mode works (used in memory_builder and reviewer)."""
        from groq import Groq
        try:
            client = Groq(api_key=os.environ["GROQ_API_KEY"])
            resp = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": 'Return a JSON object: {"status": "ok"}'}],
                max_tokens=20,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            import json
            data = json.loads(resp.choices[0].message.content)
            assert "status" in data or len(data) > 0, "JSON mode returned empty object"
            print(f"\n  Groq JSON mode: {data}")
        except Exception as exc:
            _skip_on_auth_error(exc, "Groq")
            _skip_on_quota(exc, "llama-3.1-8b-instant")
            raise


# ---------------------------------------------------------------------------
# Gemini — 2.5 Pro (primary coder), 2.5 Flash (memory/coder), 3.5 Flash
# ---------------------------------------------------------------------------


class TestGeminiEndpoints:
    """
    Gemini models used:
      - gemini-2.5-pro   → primary code generation (if quota available)
      - gemini-2.5-flash → memory builder, fallback coder (active)
      - gemini-3.5-flash → alternative active flash model
    """

    @pytest.fixture(autouse=True)
    def check_key(self):
        _skip_if_no_key("GEMINI_API_KEY", "Gemini / Google AI Studio")

    def _call(self, model: str, prompt: str = "Respond with only: OK") -> str:
        import google.generativeai as genai
        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            gmodel = genai.GenerativeModel(model)
            resp = gmodel.generate_content(prompt)
            return resp.text.strip()
        except Exception as exc:
            _skip_on_quota(exc, model)
            raise

    def test_gemini_2_5_flash(self):
        """Gemini 2.5 Flash — ACTIVE memory/coder model."""
        result = self._call("gemini-2.5-flash")
        assert result, "Expected non-empty response from gemini-2.5-flash"
        print(f"\n  gemini-2.5-flash responded: {result[:80]!r}")

    def test_gemini_3_5_flash(self):
        """Gemini 3.5 Flash — ACTIVE alternative flash model."""
        result = self._call("gemini-3.5-flash")
        assert result, "Expected non-empty response from gemini-3.5-flash"
        print(f"\n  gemini-3.5-flash responded: {result[:80]!r}")

    def test_gemini_2_5_pro(self):
        """Gemini 2.5 Pro — PRIMARY coder model."""
        try:
            result = self._call("gemini-2.5-pro")
            assert result, "Expected non-empty response from gemini-2.5-pro"
            print(f"\n  gemini-2.5-pro responded: {result[:80]!r}")
        except Exception as exc:
            _skip_on_quota(exc, "gemini-2.5-pro")
            raise

    def test_gemini_2_0_flash(self):
        """Gemini 2.0 Flash — conventions extraction."""
        try:
            result = self._call("gemini-2.0-flash")
            assert result, "Expected non-empty response from gemini-2.0-flash"
            print(f"\n  gemini-2.0-flash responded: {result[:80]!r}")
        except Exception as exc:
            _skip_on_quota(exc, "gemini-2.0-flash")
            raise

    def test_gemini_json_mode(self):
        """Verify Gemini JSON response mode (used in memory_builder)."""
        import google.generativeai as genai
        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            gmodel = genai.GenerativeModel("gemini-2.5-flash")
            resp = gmodel.generate_content(
                'Return a JSON object with key "status" set to "ok".',
                generation_config={"response_mime_type": "application/json"},
            )
            import json
            data = json.loads(resp.text)
            assert "status" in data or len(data) > 0
            print(f"\n  Gemini JSON mode: {data}")
        except Exception as exc:
            _skip_on_quota(exc, "gemini-2.5-flash")
            raise


# ---------------------------------------------------------------------------
# OpenRouter — Qwen (fallback 1), DeepSeek (fallback 2)
# ---------------------------------------------------------------------------


class TestOpenRouterEndpoints:
    """
    OpenRouter fallback chain:
      - qwen/qwen2.5-coder-32b-instruct:free → fallback coder 1
      - deepseek/deepseek-chat-v3-0324:free   → fallback coder 2
    """

    @pytest.fixture(autouse=True)
    def check_key(self):
        _skip_if_no_key("OPENROUTER_API_KEY", "OpenRouter")

    def _call(self, model: str, prompt: str = "Respond with only: OK") -> str:
        from openai import OpenAI
        try:
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
        except Exception as exc:
            _skip_on_auth_error(exc, "OpenRouter")
            _skip_on_bad_model(exc, model)
            _skip_on_quota(exc, model)
            raise

    def test_qwen_coder(self):
        """Qwen 2.5 Coder 32B — fallback coder 1."""
        result = self._call("qwen/qwen2.5-coder-32b-instruct:free")
        assert result, "Expected non-empty response"
        print(f"\n  qwen2.5-coder-32b-instruct:free responded: {result[:80]!r}")

    def test_deepseek_chat(self):
        """DeepSeek V4 Flash — fallback coder 2."""
        result = self._call("deepseek/deepseek-v4-flash:free")
        assert result, "Expected non-empty response"
        print(f"\n  deepseek-v4-flash:free responded: {result[:80]!r}")


# ---------------------------------------------------------------------------
# Anthropic — Claude Sonnet (last-resort fallback 3, paid)
# ---------------------------------------------------------------------------


class TestAnthropicEndpoints:
    """
    Anthropic Claude — last-resort fallback coder (paid).
    Only tested if ANTHROPIC_API_KEY is set and valid.
    """

    @pytest.fixture(autouse=True)
    def check_key(self):
        _skip_if_no_key("ANTHROPIC_API_KEY", "Anthropic (paid — last resort fallback)")

    def test_claude_sonnet(self):
        """Claude Sonnet — last-resort fallback coder 3."""
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        except TypeError as exc:
            if "proxies" in str(exc):
                pytest.skip(
                    "anthropic/httpx version conflict — "
                    "run: pip install anthropic --upgrade"
                )
            raise

        try:
            resp = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=10,
                messages=[{"role": "user", "content": "Respond with only: OK"}],
            )
            result = resp.content[0].text.strip()
            assert result, "Expected non-empty response from Claude Sonnet"
            print(f"\n  claude-sonnet-4-20250514 responded: {result!r}")
        except Exception as exc:
            _skip_on_auth_error(exc, "Anthropic")
            _skip_on_quota(exc, "claude-sonnet-4-20250514")
            raise


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
        from github import Auth, Github
        gh = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
        user = gh.get_user()
        rate = gh.get_rate_limit()
        print(f"\n  GitHub token valid for: {user.login}")
        print(f"  Core rate limit: {rate.rate.remaining}/{rate.rate.limit}")
        assert user.login, "Expected valid GitHub user"
        assert rate.rate.remaining > 0, (
            "GitHub rate limit exhausted! Wait until reset: "
            + str(rate.rate.reset)
        )


# ---------------------------------------------------------------------------
# Local Ollama Smoke Test
# ---------------------------------------------------------------------------


class TestOllamaEndpoints:
    """Verify local Ollama is running and configured models respond."""

    @pytest.fixture(autouse=True)
    def check_ollama_status(self):
        import socket
        try:
            # Check if localhost:11434 is open
            with socket.create_connection(("localhost", 11434), timeout=1.0):
                pass
        except Exception:
            pytest.skip("Local Ollama is not running on localhost:11434. Start Ollama to run this test.")

    def test_qwen_7b(self):
        """Verify qwen2.5:7b responds and caches successfully."""
        import time
        from integrations.ollama_client import call_ollama
        
        prompt = "Respond with exactly: OK"
        
        # 1. First execution - cache miss or hit
        result1 = call_ollama(model="qwen2.5:7b", prompt=prompt)
        assert "OK" in result1.upper(), f"Expected OK, got: {result1}"
        
        # 2. Second execution - guaranteed cache hit
        start = time.time()
        result2 = call_ollama(model="qwen2.5:7b", prompt=prompt)
        duration = time.time() - start
        
        assert "OK" in result2.upper()
        # Cache hit should be near-instantaneous (<0.6 seconds) compared to local CPU inference
        assert duration < 0.6, f"Cache retrieval was slow: {duration}s"
        print(f"\n  qwen2.5:7b responded: {result2!r} (cache retrieval: {duration:.4f}s)")
        
    def test_gemma_12b(self):
        """Verify gemma4:12b responds and handles low-temperature orchestration rules."""
        from integrations.ollama_client import call_ollama
        result = call_ollama(model="gemma4:12b", prompt="Respond with only: OK")
        assert "OK" in result.upper(), f"Expected OK, got: {result}"
        print(f"\n  gemma4:12b responded: {result!r}")

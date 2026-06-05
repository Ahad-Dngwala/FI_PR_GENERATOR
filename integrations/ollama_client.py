import os
from typing import Optional
import structlog

log = structlog.get_logger(__name__)


def call_ollama(
    model: str,
    prompt: str,
    messages: Optional[list[dict]] = None,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    response_format: Optional[dict] = None,
) -> str:
    """
    Call local Ollama model via OpenAI-compatible endpoint.
    Includes serialization, prompt response caching, and timeout checks.
    """
    from openai import OpenAI
    from integrations.local_queue import call_ollama_serialized

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    client = OpenAI(base_url=base_url, api_key="ollama")

    if not messages:
        messages = [{"role": "user", "content": prompt}]

    # Optimization parameters for local orchestration:
    # low temperature for determinism, specific top_p and top_k to control search
    extra_body = {
        "options": {
            "top_p": 0.8,
            "top_k": 64,
        }
    }

    # Define actual HTTP call closure to pass to local queue serializer
    def _client_call(
        model: str,
        messages: list,
        temperature: float,
        response_format: Optional[dict],
        max_tokens: Optional[int],
        extra_body: Optional[dict],
        timeout: int,
    ) -> str:
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": timeout,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if response_format is not None:
            kwargs["response_format"] = response_format
        if extra_body is not None:
            kwargs["extra_body"] = extra_body

        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    # Execute serialized and cached call
    return call_ollama_serialized(
        client_fn=_client_call,
        model=model,
        messages=messages,
        temperature=temperature,
        response_format=response_format,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )

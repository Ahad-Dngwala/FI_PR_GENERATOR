import json
import hashlib
import threading
import time
import os
from pathlib import Path
import structlog

log = structlog.get_logger(__name__)

CACHE_PATH = Path("memory_store/ollama_cache.json")
_cache_lock = threading.Lock()
_ollama_lock = threading.Lock()

OLLAMA_TIMEOUT_SECONDS = 120
OLLAMA_MAX_CONCURRENT = 1


def get_cache_key(model: str, messages: list) -> str:
    """Generate a SHA256 hex digest key for caching."""
    serialized = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def read_cache() -> dict:
    """Read prompt cache file. Safe to call from multiple threads under _cache_lock."""
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("local_queue.cache_read_failed", error=str(exc))
        return {}


def write_cache(cache_data: dict) -> None:
    """Write prompt cache file. Safe to call from multiple threads under _cache_lock."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.warning("local_queue.cache_write_failed", error=str(exc))


def call_ollama_serialized(
    client_fn,
    model: str,
    messages: list,
    temperature: float = 0.2,
    response_format: dict = None,
    max_tokens: int = None,
    extra_body: dict = None,
) -> str:
    """
    Serialize Ollama requests via thread locks, cache responses to avoid CPU hit,
    enforce timeout, and monitor latency.
    """
    key = get_cache_key(model, messages)

    # 1. Try reading from cache first (no lock-waiting for Ollama if cache hit)
    with _cache_lock:
        cache = read_cache()
        if key in cache:
            log.info("local_queue.cache_hit", model=model, key=key)
            return cache[key]

    # 2. Serialize Ollama invocation (only 1 thread can invoke local model at a time)
    log.info("local_queue.acquire_lock", model=model, timeout=OLLAMA_TIMEOUT_SECONDS)
    start_time = time.time()

    with _ollama_lock:
        log.info("local_queue.lock_acquired", model=model)
        
        # Enforce timeout inside call via client or wrap execution
        response_text = client_fn(
            model=model,
            messages=messages,
            temperature=temperature,
            response_format=response_format,
            max_tokens=max_tokens,
            extra_body=extra_body,
            timeout=OLLAMA_TIMEOUT_SECONDS
        )

        latency = time.time() - start_time
        log.info(
            "local_queue.execution_done",
            model=model,
            latency_seconds=round(latency, 2),
        )

    # 3. Save response to persistent cache
    with _cache_lock:
        cache = read_cache()
        cache[key] = response_text
        write_cache(cache)

    return response_text

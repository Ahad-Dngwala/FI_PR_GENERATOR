import json
from pathlib import Path

CONFIG_PATH = Path("config/models.json")


def load_models_config() -> dict:
    """Load and parse config/models.json. Returns empty dict on failure."""
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def get_model_name(key: str, default: str) -> str:
    """Get the model name for a specific key, falling back to default."""
    config = load_models_config()
    return config.get(key, default)


def get_model_provider(key: str, default: str) -> str:
    """Get the provider name for a specific key, falling back to default."""
    config = load_models_config()
    return config.get(key, default)


def get_coding_chain() -> list[tuple[str, str]]:
    """Get the coding fallback chain as a list of (model, provider) tuples."""
    config = load_models_config()
    chain = config.get("coding_chain", [])
    if chain:
        return [(c["model"], c["provider"]) for c in chain]
    return [
        ("gemini/gemini-2.5-pro", "google"),
        ("qwen/qwen-2.5-coder-72b-instruct", "openrouter"),
        ("deepseek/deepseek-coder-v2", "openrouter"),
        ("claude-sonnet-4-20250514", "anthropic"),
    ]

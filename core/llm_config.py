"""Single source of truth for the AI vision endpoint (base URL + API key).

Backed by os.environ rather than a module-level constant, on purpose: both
app.py and core/microstock.py need to see the same value, and both already
read OMLX_API_KEY live via os.environ.get() at call time. Using os.environ
as the shared mutable store means set_llm_config() (called from the
/api/llm-config settings route) takes effect immediately for every caller in
the process, with no import-order dependency between modules and no need to
plumb a reference through every function signature.
"""

import json
import os

DEFAULT_BASE_URL = "https://api.openai.com"


def get_base_url() -> str:
    return os.environ.get("TOTAG_LLM_BASE_URL", DEFAULT_BASE_URL)


def get_chat_url() -> str:
    return f"{get_base_url().rstrip('/')}/v1/chat/completions"


def get_api_key() -> str:
    return os.environ.get("OMLX_API_KEY", "")


def set_llm_config(base_url: str | None = None, api_key: str | None = None) -> None:
    """Update the live config. base_url='' resets to the default; api_key is
    only overwritten when non-empty (blank means "keep the existing key")."""
    if base_url is not None:
        if base_url:
            os.environ["TOTAG_LLM_BASE_URL"] = base_url
        else:
            os.environ.pop("TOTAG_LLM_BASE_URL", None)
    if api_key:
        os.environ["OMLX_API_KEY"] = api_key


def load_saved_config(path: str) -> None:
    """Load a previously saved {base_url, api_key} from `path`, if present,
    applying it over whatever TOTAG_LLM_BASE_URL/OMLX_API_KEY were seeded
    from the environment at process start. Safe to call even if the file
    doesn't exist yet (first run)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if isinstance(data, dict):
        set_llm_config(data.get("base_url"), data.get("api_key"))


def save_config(path: str, base_url: str, api_key: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"base_url": base_url, "api_key": api_key}, f, indent=2, ensure_ascii=False)

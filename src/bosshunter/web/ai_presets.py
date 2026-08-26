"""Persistent AI setting presets for the web dashboard.

Each preset stores a named snapshot of the AI configuration (provider, service,
model, base_url, credentials, thinking options, timeouts, etc.) so users can
switch between different AI setups from the config page without re-entering them.
"""

import json
from pathlib import Path
from threading import Lock

from bosshunter.config import AI_SERVICE_PRESETS


# The subset of ai.* config fields that define a preset.
# Fields that are purely runtime tuning knobs (e.g. per-request budgets) are
# intentionally included so a preset fully restores the intended setup.
_PRESET_KEYS = (
    "provider",
    "service",
    "model",
    "base_url",
    "api_key",
    "auth_token",
    "thinking",
    "thinking_budget",
    "timeout_seconds",
    "scoring_max_tokens",
    "scoring_max_attempts",
    "greeting_max_tokens",
    "greeting_review_max_tokens",
    "greeting_max_attempts",
    "greeting_review_threshold",
    "greeting_max_iterations",
)

_lock = Lock()


def _presets_path() -> Path:
    """Resolve the presets file relative to the runtime base dir."""
    from bosshunter.web.server import BASE_DIR

    data_dir = BASE_DIR / "data"
    return data_dir / "ai_presets.json"


def _load_presets() -> dict[str, dict]:
    """Load all saved presets keyed by alias name."""
    path = _presets_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(name): settings
        for name, settings in data.items()
        if isinstance(settings, dict)
    }


def _save_presets(presets: dict[str, dict]) -> None:
    """Persist the presets dict to disk, creating parent dirs as needed."""
    path = _presets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(presets, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _mask(value) -> str:
    if not value:
        return ""
    value = str(value)
    if len(value) > 8:
        return value[:4] + "***" + value[-4:]
    return "***"


def _redact_preset(preset: dict) -> dict:
    """Return a display-safe copy of a preset (masked credentials)."""
    redacted = dict(preset)
    for field in ("api_key", "auth_token"):
        raw = preset.get(field)
        if raw:
            redacted[field + "_masked"] = _mask(raw)
            redacted[field] = ""
        else:
            redacted.pop(field, None)
    return redacted


def list_presets() -> list[dict]:
    """Return all presets, masked for the browser, newest first."""
    with _lock:
        presets = _load_presets()
    items = [
        {
            "name": name,
            "settings": _redact_preset(settings),
            "updated_at": settings.get("updated_at", ""),
        }
        for name, settings in presets.items()
    ]
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return items


def get_preset(name: str) -> dict | None:
    """Return a single preset by name (credentials included for direct use)."""
    with _lock:
        presets = _load_presets()
    preset = presets.get(name)
    if preset is None:
        return None
    return {key: value for key, value in preset.items() if key in _PRESET_KEYS}


def _extract_ai_settings(config: dict) -> dict:
    """Pull the preset-relevant fields from a config dict."""
    ai_cfg = config.get("ai", {}) if isinstance(config.get("ai"), dict) else {}
    settings: dict = {}
    for key in _PRESET_KEYS:
        if key in ai_cfg and ai_cfg[key] not in (None, ""):
            settings[key] = ai_cfg[key]

    # Normalize service/provider so legacy configs are stored consistently.
    service = settings.get("service")
    if service not in AI_SERVICE_PRESETS:
        provider = settings.get("provider")
        service = "custom" if provider == "openai_compatible" else "anthropic"
        settings["service"] = service
    settings["provider"] = AI_SERVICE_PRESETS[service]["provider"]
    return settings


def save_preset(name: str, config: dict) -> dict:
    """Save the current AI settings as a preset under the given alias name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("别名名称不能为空")
    if len(name) > 50:
        raise ValueError("别名名称过长（最多 50 个字符）")

    settings = _extract_ai_settings(config)
    settings["updated_at"] = _now_iso()

    with _lock:
        presets = _load_presets()
        presets[name] = settings
        _save_presets(presets)
    return {"name": name, "settings": _redact_preset(settings)}


def delete_preset(name: str) -> bool:
    """Delete a preset by name. Returns True if it existed."""
    with _lock:
        presets = _load_presets()
        if name not in presets:
            return False
        presets.pop(name)
        _save_presets(presets)
    return True


def _now_iso() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

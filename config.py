r"""
Kelpie Global Configuration.

A single, project-wide source of truth for variables shared across modules
(the task data root, the global log directory, and logging preferences).
Previously these were scattered as module-level constants (e.g.
``DEFAULT_DATA_ROOT`` in ``Tasks/task_runner.py`` and ``LOG_DIR`` /
``log.config`` in ``Logger/kelpie_logger.py``); they now live here.

Format: JSON (``kelpie_config.json``, co-located with this module). JSON is
used for consistency with the rest of the project (TaskConfig.json,
ollama_config.json, task_execution_state.json) and because it needs no
third-party dependency.

Environment overrides
---------------------
Selected values may be overridden per-run via environment variables, which is
handy for dev/prod parity without editing the file:

    NUMBAT_DATA_ROOT   -> data_root
    KELPIE_LOG_DIR     -> log_dir

Note on mutable state
---------------------
This file holds only *static, human-edited* settings. The incrementing logging
``session_counter`` is runtime state, not configuration, and is persisted
separately by the logger (``Logger/log_state.json``) so this shared config is
never rewritten on every run.
"""

import json
import os

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(CONFIG_DIR, "kelpie_config.json")

# Fallback defaults, used only if a key is missing or the file cannot be read.
_DEFAULTS = {
    "data_root": r"D:\Data\Numbat",
    "log_dir": r"D:\Logs\KelpieLogs",
    "save_script_name": False,
}

# Environment variable -> config key overrides.
_ENV_OVERRIDES = {
    "data_root": "NUMBAT_DATA_ROOT",
    "log_dir": "KELPIE_LOG_DIR",
}

# Cache so the file is read at most once per process.
_config = None


def _load() -> dict:
    """Read kelpie_config.json once, layering file values over the defaults."""
    global _config
    if _config is not None:
        return _config

    merged = dict(_DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            merged.update(data)
    except FileNotFoundError:
        # Fall back to defaults; the project still runs without the file.
        pass
    except json.JSONDecodeError:
        # Malformed config should not crash logging/task discovery.
        pass

    _config = merged
    return _config


def get(key: str, default=None):
    """Return a config value, honoring an env-var override when defined."""
    env_name = _ENV_OVERRIDES.get(key)
    if env_name:
        env_value = os.environ.get(env_name)
        if env_value is not None:
            return env_value
    return _load().get(key, default)


def get_data_root() -> str:
    """Root folder of the profile-based task data (e.g. ``D:\\Data\\Numbat``)."""
    return get("data_root", _DEFAULTS["data_root"])


def get_log_dir() -> str:
    """Directory for the global Kelpie log files."""
    return get("log_dir", _DEFAULTS["log_dir"])


def get_save_script_name() -> bool:
    """Whether log lines include the calling script name."""
    value = get("save_script_name", _DEFAULTS["save_script_name"])
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")

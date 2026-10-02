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
per profile by the task runner in ``<profile_dir>/Task_Config.json`` so this
shared config is never rewritten on every run.
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


# ---------------------------------------------------------------------------
# Task identifier encoding / decoding
# ---------------------------------------------------------------------------
#
# Messages published between Kelpie's components (web-extract, local-AI and
# fallback queues) carry a ``task_identifier`` that names the run. Historically
# this was the *entire absolute path* of the run's InProgress session folder::
#
#     <data_root>/<profile_name>/Tasks/<task_name>/InProgress/<session_counter>
#
# That leaks the on-disk layout onto the wire and is brittle. Instead the wire
# now carries a compact, path-free token::
#
#     "{profile_name}|{task_name}|{session_counter}"
#
# Consumers decode it back to the absolute session folder path (the mapping is
# fully deterministic given the data root), so all existing filesystem logic
# keeps working unchanged. Legacy absolute paths (no ``|``) are still accepted
# for backward compatibility.

#: Delimiter between the three parts of a compact task identifier.
TASK_IDENTIFIER_SEPARATOR = "|"

#: Path segment (under the profile folder) that holds a profile's tasks.
PROFILE_TASKS_SUBDIR = "Tasks"

#: Path segment (under a task folder) that holds in-progress session folders.
INPROGRESS_DIRNAME = "InProgress"


def encode_task_identifier(profile_name: str, task_name: str, session_counter) -> str:
    """Build the compact ``"{profile_name}|{task_name}|{session_counter}"`` token.

    Args:
        profile_name: The owning profile (the folder directly under the data
            root, e.g. an email).
        task_name: The task's folder name.
        session_counter: The InProgress session counter for this run.

    Returns:
        The compact task identifier carried on the wire.
    """
    return (
        f"{profile_name}{TASK_IDENTIFIER_SEPARATOR}"
        f"{task_name}{TASK_IDENTIFIER_SEPARATOR}{session_counter}"
    )


def is_compact_task_identifier(value: str) -> bool:
    """Return True if ``value`` looks like a compact token (contains the sep)."""
    return isinstance(value, str) and TASK_IDENTIFIER_SEPARATOR in value


def decode_task_identifier(value: str) -> str:
    """Resolve a task identifier to the absolute InProgress session folder path.

    Accepts either a compact ``"{profile_name}|{task_name}|{session_counter}"``
    token (resolved against the data root into
    ``<data_root>/<profile_name>/Tasks/<task_name>/InProgress/<session_counter>``)
    or a legacy absolute session-folder path (returned unchanged), so producers
    and consumers can be upgraded independently.

    Args:
        value: A compact token or a legacy absolute path.

    Returns:
        The absolute path of the InProgress session folder.

    Raises:
        ValueError: If a compact token does not split into exactly three parts.
    """
    if not is_compact_task_identifier(value):
        # Legacy absolute path (or already-resolved path): use as-is.
        return value

    # maxsplit=2 so a stray separator inside a task name never bleeds into the
    # session_counter; the counter is always the final segment.
    parts = value.split(TASK_IDENTIFIER_SEPARATOR, 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise ValueError(
            f"malformed task identifier {value!r}: expected "
            f"'profile_name{TASK_IDENTIFIER_SEPARATOR}task_name"
            f"{TASK_IDENTIFIER_SEPARATOR}session_counter'"
        )

    profile_name, task_name, session_counter = (part.strip() for part in parts)
    return os.path.join(
        get_data_root(),
        profile_name,
        PROFILE_TASKS_SUBDIR,
        task_name,
        INPROGRESS_DIRNAME,
        str(session_counter),
    )


def task_identifier_from_session_dir(session_dir: str) -> str:
    """Derive the compact token from an absolute InProgress session-folder path.

    Inverse of :func:`decode_task_identifier` for the common case where a
    consumer holds the resolved ``session_dir`` and needs to re-publish. The
    path is expected to end with
    ``.../<profile_name>/Tasks/<task_name>/InProgress/<session_counter>``.

    If the path does not have that shape it is returned unchanged so callers
    degrade gracefully to the legacy behaviour.
    """
    normalized = os.path.normpath(session_dir)
    parts = normalized.split(os.sep)
    # Need at least: profile, "Tasks", task, "InProgress", counter.
    if len(parts) >= 5 and parts[-2] == INPROGRESS_DIRNAME and parts[-4] == PROFILE_TASKS_SUBDIR:
        profile_name = parts[-5]
        task_name = parts[-3]
        session_counter = parts[-1]
        return encode_task_identifier(profile_name, task_name, session_counter)
    return session_dir

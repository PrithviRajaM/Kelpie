r"""
Kelpie Logger - Reusable logging utility.

Logs are written to the directory configured as ``log_dir`` in the project's
global config (``kelpie_config.json``); default ``D:\Logs\KelpieLogs``, one
file per day. Each log line format:
``HH:MM:SS [session_counter] <source_script> <message>``.

The session counter is set externally by the caller via
``set_session_counter``. The logger itself no longer owns, derives, or
persists the counter; it simply stamps whatever value has been set into each
log line. The counter is now derived and persisted per profile by the task
runner in ``<profile_dir>/Task_Config.json`` (per qualified task run).

Whether the source script name is included in a log line is controlled by the
``save_script_name`` setting in the global config (``kelpie_config.json``).
"""

import json
import os
import sys
from datetime import datetime

# Import the project-wide config. The project root is one level up from Logger/.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config as kelpie_config


# Global log directory now comes from the centralized config.
LOG_DIR = kelpie_config.get_log_dir()

# Session counter stamped into each log line. This value is set externally by
# the caller (the task runner) via ``set_session_counter``; the logger no
# longer derives or persists it.
_session_counter = 0

# Cached value of the save_script_name property (from the global config).
_save_script_name = True


def _load_config():
    """Load save_script_name (from global config)."""
    global _save_script_name
    _save_script_name = kelpie_config.get_save_script_name()


def set_session_counter(counter: int) -> None:
    """Set the session counter stamped into subsequent log lines.

    The counter is derived and persisted by the task runner (in
    ``Tasks/task_execution_state.json``). Calling this makes all following log
    lines carry ``counter`` until it is set again. Also refreshes the cached
    ``save_script_name`` setting from the global config.

    Args:
        counter: The session counter value to stamp into log lines.
    """
    global _session_counter, _save_script_name
    try:
        _session_counter = int(counter)
    except (ValueError, TypeError):
        _session_counter = 0
    _save_script_name = kelpie_config.get_save_script_name()


def _ensure_log_dir():
    """Create the log directory if it does not exist."""
    os.makedirs(LOG_DIR, exist_ok=True)


def _get_log_file_path():
    """Return the path to today's log file (one file per day)."""
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"kelpie_{today}.log")


def _format_line(source: str, message: str) -> str:
    """Format a single log line with timestamp, session counter, source, and message.

    The session counter is enclosed in square braces and placed between the
    source and the message. The source is only included when the
    ``save_script_name`` config property is enabled.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    counter = f"[{_session_counter}]"
    if _save_script_name:
        return f"{timestamp} {source} {counter} {message}"
    return f"{timestamp} {counter} {message}"


def log_info(source: str, message: str):
    """Log an informational message.

    Args:
        source: Name of the calling script (e.g. 'main.py').
        message: The log message to record.
    """
    _ensure_log_dir()
    line = _format_line(source, f"[INFO] {message}")
    _write(line)


def log_warning(source: str, message: str):
    """Log a warning message.

    Args:
        source: Name of the calling script.
        message: The log message to record.
    """
    _ensure_log_dir()
    line = _format_line(source, f"[WARN] {message}")
    _write(line)


def log_error(source: str, message: str):
    """Log an error message.

    Args:
        source: Name of the calling script.
        message: The log message to record.
    """
    _ensure_log_dir()
    line = _format_line(source, f"[ERROR] {message}")
    _write(line)


def log_debug(source: str, message: str):
    """Log a debug message.

    Args:
        source: Name of the calling script.
        message: The log message to record.
    """
    _ensure_log_dir()
    line = _format_line(source, f"[DEBUG] {message}")
    _write(line)


def log_critical(source: str, message: str):
    """Log a critical message.

    Args:
        source: Name of the calling script.
        message: The log message to record.
    """
    _ensure_log_dir()
    line = _format_line(source, f"[CRITICAL] {message}")
    _write(line)


def _write(line: str):
    """Append a formatted line to today's log file."""
    filepath = _get_log_file_path()
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Per-task logging
#
# In addition to the global Kelpie log, each task keeps its own log inside its
# profile folder: ``<task_dir>/Logs/task_<YYYY-MM-DD>.log`` (one file per date).
# Task log lines reuse the same session counter (the run id) and the same
# ``HH:MM:SS [<run_id>] [LEVEL] <message>`` format as the global log, so the
# NumbatAPI/NumbatUI log parser can read them unchanged.
# ---------------------------------------------------------------------------

# Filename prefix/date pattern expected by the log reader (see TASKS.md §6.1).
_TASK_LOG_PREFIX = "task_"


def _task_log_file_path(logs_dir: str) -> str:
    """Return the path to today's per-task log file inside ``logs_dir``."""
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(logs_dir, f"{_TASK_LOG_PREFIX}{today}.log")


def _write_task(logs_dir: str, line: str):
    """Append a formatted line to today's log file inside a task's Logs folder.

    The ``Logs`` folder is created if it does not already exist. Failures are
    swallowed so per-task logging can never abort a running task; the global
    log remains the source of truth for hard errors.
    """
    try:
        os.makedirs(logs_dir, exist_ok=True)
        filepath = _task_log_file_path(logs_dir)
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log_task(logs_dir: str, level: str, source: str, message: str):
    """Write one line to a task's own log file (``<logs_dir>/task_<date>.log``).

    Args:
        logs_dir: The task's ``Logs`` folder. Created if missing.
        level: Severity keyword, e.g. ``"INFO"``, ``"WARN"``, ``"ERROR"``.
        source: Name of the calling script (included only when
            ``save_script_name`` is enabled, matching the global log).
        message: The log message to record.
    """
    line = _format_line(source, f"[{level.upper()}] {message}")
    _write_task(logs_dir, line)


def log_task_info(logs_dir: str, source: str, message: str):
    """Log an informational line to a task's own log file."""
    log_task(logs_dir, "INFO", source, message)


def log_task_warning(logs_dir: str, source: str, message: str):
    """Log a warning line to a task's own log file."""
    log_task(logs_dir, "WARN", source, message)


def log_task_error(logs_dir: str, source: str, message: str):
    """Log an error line to a task's own log file."""
    log_task(logs_dir, "ERROR", source, message)


# Load current config values on import so that any logging performed before
# an explicit ``set_session_counter`` call still reflects the persisted
# settings (with the session counter defaulting to 0 until set).
_load_config()

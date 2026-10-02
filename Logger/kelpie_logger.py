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

# Active task session. When a task session is active (see ``begin_task_session``)
# this holds the task's ``Logs`` folder; while set, the ordinary global log
# functions (``log_info``/``log_warning``/``log_error``/``log_debug``/
# ``log_critical``) redirect their output into that task's own log instead of
# the shared global log. This is what makes "once the session counter is
# derived, everything until the task completes logs against the task" hold for
# every nested call across Ollama, Swagman and Messaging without any of those
# modules having to be aware of the task's Logs folder. It is cleared by
# ``end_task_session`` so logging reverts to the global log afterwards.
_active_task_logs_dir = None


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


def begin_task_session(logs_dir: str) -> None:
    """Route all subsequent logging into a task's own log until it completes.

    Once the caller has derived the session counter (via
    ``set_session_counter``) and knows the task's ``Logs`` folder, it calls this
    to activate the task session. While a session is active, the ordinary global
    log functions (``log_info``/``log_warning``/``log_error``/``log_debug``/
    ``log_critical``) transparently write into ``<logs_dir>/task_<date>.log``
    instead of the shared global log. This means every nested call made during
    the run - including code in Ollama, Swagman and Messaging that only knows
    about the plain ``log_*`` helpers - is captured against the task.

    Call ``end_task_session`` (ideally in a ``finally``) when the run completes
    so logging reverts to the global log. Nested/re-entrant calls are not
    stacked: the most recent ``begin_task_session`` wins and a single
    ``end_task_session`` clears the active session.

    Args:
        logs_dir: The task's ``Logs`` folder (created on first write).
    """
    global _active_task_logs_dir
    _active_task_logs_dir = logs_dir


def end_task_session() -> None:
    """Deactivate the active task session so logging reverts to the global log."""
    global _active_task_logs_dir
    _active_task_logs_dir = None


def active_task_logs_dir():
    """Return the active task's ``Logs`` folder, or None when no session is active."""
    return _active_task_logs_dir


class task_session:
    """Context manager wrapper around ``begin_task_session``/``end_task_session``.

    Usage::

        with task_session(task.logs_dir):
            ...  # every log_* call in here (and in anything it calls) is
                 # written to the task log
    """

    def __init__(self, logs_dir: str):
        self._logs_dir = logs_dir

    def __enter__(self):
        begin_task_session(self._logs_dir)
        return self

    def __exit__(self, exc_type, exc, tb):
        end_task_session()
        return False


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


def _emit(level: str, source: str, message: str):
    """Write one log line, routed to the active task log when a session is active.

    This is the single funnel every global log function goes through. When a
    task session is active (``begin_task_session`` was called and not yet
    ended), the line is written to that task's own log so all logging for the
    run - including nested calls from other modules - is captured against the
    task. Otherwise the line goes to the shared global log as before.
    """
    line = _format_line(source, f"[{level}] {message}")
    if _active_task_logs_dir is not None:
        _write_task(_active_task_logs_dir, line)
    else:
        _ensure_log_dir()
        _write(line)


def log_info(source: str, message: str):
    """Log an informational message.

    Args:
        source: Name of the calling script (e.g. 'main.py').
        message: The log message to record.
    """
    _emit("INFO", source, message)


def log_warning(source: str, message: str):
    """Log a warning message.

    Args:
        source: Name of the calling script.
        message: The log message to record.
    """
    _emit("WARN", source, message)


def log_error(source: str, message: str):
    """Log an error message.

    Args:
        source: Name of the calling script.
        message: The log message to record.
    """
    _emit("ERROR", source, message)


def log_debug(source: str, message: str):
    """Log a debug message.

    Args:
        source: Name of the calling script.
        message: The log message to record.
    """
    _emit("DEBUG", source, message)


def log_critical(source: str, message: str):
    """Log a critical message.

    Args:
        source: Name of the calling script.
        message: The log message to record.
    """
    _emit("CRITICAL", source, message)


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

    The ``Logs`` folder is created if it does not already exist. If the per-task
    write fails (e.g. ``logs_dir`` is invalid, unwritable, or contains illegal
    path characters), the failure is not swallowed silently: the original line
    plus a diagnostic note are redirected to the global log so the problem is
    visible and the line is not lost. Per-task logging still never aborts a
    running task - a failure of the global fallback itself is the only thing
    swallowed, as a last resort.
    """
    try:
        os.makedirs(logs_dir, exist_ok=True)
        filepath = _task_log_file_path(logs_dir)
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        # The task log could not be written; fall back to the global log so the
        # line survives and the misconfiguration surfaces instead of vanishing.
        try:
            _ensure_log_dir()
            note = _format_line(
                __name__,
                f"[ERROR] Failed to write task log to '{logs_dir}': "
                f"{type(exc).__name__}: {exc}. Original line redirected here.",
            )
            _write(note)
            _write(line)
        except OSError:
            # Even the global log is unavailable; give up rather than crash the
            # running task.
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


def log_task_debug(logs_dir: str, source: str, message: str):
    """Log a debug line to a task's own log file."""
    log_task(logs_dir, "DEBUG", source, message)


def log_task_critical(logs_dir: str, source: str, message: str):
    """Log a critical line to a task's own log file."""
    log_task(logs_dir, "CRITICAL", source, message)


def begin_task_session_for(session_dir: str) -> int:
    """Activate a task session from an ``InProgress`` session folder path.

    Consumers (Swagman/local-AI/fallback) run in their own processes and only
    receive the ``task_identifier`` - the absolute path to the run's
    ``InProgress/<session_counter>`` folder. This helper derives both pieces of
    state those processes need and activates the task session in one call:

      - the session counter, which is the session folder's own name
        (``basename(session_dir)``), so log lines carry the same run id the
        task runner assigned; and
      - the task's ``Logs`` folder, which sits two levels up from the session
        folder (``<task_dir>/Logs``), matching ``DiscoveredTask.logs_dir``.

    After this call, every ``log_*`` call in the process is routed to the
    task's own log until ``end_task_session`` is called. Callers should end the
    session in a ``finally`` once the message is handled.

    Args:
        session_dir: Absolute path to the ``InProgress/<session_counter>`` folder.

    Returns:
        The derived session counter (0 if it could not be parsed).
    """
    normalized = os.path.normpath(session_dir)
    task_dir = os.path.dirname(os.path.dirname(normalized))
    logs_dir = os.path.join(task_dir, "Logs")

    try:
        counter = int(os.path.basename(normalized))
    except (ValueError, TypeError):
        counter = 0

    set_session_counter(counter)
    begin_task_session(logs_dir)
    return counter


# Load current config values on import so that any logging performed before
# an explicit ``set_session_counter`` call still reflects the persisted
# settings (with the session counter defaulting to 0 until set).
_load_config()

r"""
Kelpie Logger - Reusable logging utility.

Logs are written to D:\Logs\KelpieLogs with one file per day.
Each log line format: HH:MM:SS [session_counter] <source_script> <message>

The session counter is a per-session value that stays constant for the
lifetime of a single scheduled execution (session). It is incremented once
when the session starts (see ``start_session``) and persisted to
``log.config`` so the next session continues from the last value.

Whether the source script name is included in a log line is controlled by
the ``save_script_name`` property in ``log.config``.
"""

import os
from datetime import datetime


LOG_DIR = r"D:\Logs\KelpieLogs"

# Path to the logger configuration file (co-located with this module).
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log.config")

# Session counter for the current process. Loaded/incremented by
# ``start_session`` and held constant for all logging within the session.
_session_counter = 0

# Cached value of the save_script_name property.
_save_script_name = True


def _read_config() -> dict:
    """Read the log.config file into a dict of string key/value pairs."""
    config = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                config[key.strip()] = value.strip()
    except OSError:
        pass
    return config


def _write_config(config: dict):
    """Write the given key/value pairs back to log.config."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        for key, value in config.items():
            f.write(f"{key}:{value}\n")


def _parse_bool(value: str) -> bool:
    """Interpret a config string value as a boolean."""
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _load_config():
    """Load the counter and save_script_name settings from config."""
    global _session_counter, _save_script_name
    config = _read_config()
    try:
        _session_counter = int(config.get("session_counter", "0"))
    except ValueError:
        _session_counter = 0
    _save_script_name = _parse_bool(config.get("save_script_name", "true"))


def start_session() -> int:
    """Begin a new logging session.

    Increments the persisted session counter by one and holds the new value
    for the lifetime of this process. All subsequent log lines use this same
    counter value. Call this once at the start of each scheduled execution.

    Returns:
        The counter value for the newly started session.
    """
    global _session_counter, _save_script_name
    config = _read_config()

    try:
        current = int(config.get("session_counter", "0"))
    except ValueError:
        current = 0

    _session_counter = current + 1
    _save_script_name = _parse_bool(config.get("save_script_name", "true"))

    config["session_counter"] = str(_session_counter)
    # Preserve save_script_name (and any other keys) on write.
    config.setdefault("save_script_name", "true" if _save_script_name else "false")
    _write_config(config)

    return _session_counter


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


# Load current config values on import so that any logging performed before
# an explicit ``start_session`` call still reflects the persisted settings.
_load_config()

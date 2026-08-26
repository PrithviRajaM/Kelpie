r"""
Kelpie Logger - Reusable logging utility.

Logs are written to D:\Logs\KelpieLogs with one file per day.
Each log line format: HH:MM:SS <source_script> <message>
"""

import os
from datetime import datetime


LOG_DIR = r"D:\Logs\KelpieLogs"


def _ensure_log_dir():
    """Create the log directory if it does not exist."""
    os.makedirs(LOG_DIR, exist_ok=True)


def _get_log_file_path():
    """Return the path to today's log file (one file per day)."""
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"kelpie_{today}.log")


def _format_line(source: str, message: str) -> str:
    """Format a single log line with timestamp, source, and message."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    return f"{timestamp} {source} {message}"


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

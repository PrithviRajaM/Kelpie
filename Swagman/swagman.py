"""
Swagman - Public entry point for the Lyrebird web-extract capability.

This script is intended to be called from external sources. It exposes a
single public method, ``extract_web_page``, which validates its inputs and
delegates to ``web_extract.run_web_extract_task``.

Usage (as a library)::

    from Swagman.swagman import extract_web_page

    result_path = extract_web_page(
        task_identifier="Extract_Coles_Menu",
        web_url="https://www.coles.com.au/browse",
        destination_folder_name="coles_item_categories",
    )

Usage (from the command line)::

    py -3 Swagman/swagman.py --task-identifier Extract_Coles_Menu \
        --web-url https://www.coles.com.au/browse \
        --destination-folder-name coles_item_categories
"""

import argparse
import os
import sys

# Resolve paths relative to the Kelpie project root (two levels up from
# Kelpie/Swagman/) so the Kelpie logger and the local web_extract module can be
# imported regardless of the working directory the caller runs from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
_LOGGER_DIR = os.path.join(PROJECT_ROOT, "Logger")
if _LOGGER_DIR not in sys.path:
    sys.path.insert(0, _LOGGER_DIR)

import kelpie_logger as logger
from web_extract import run_web_extract_task

SCRIPT_NAME = "swagman.py"

# Name of the per-task Logs folder (mirrors task_runner.TASK_LOGS_DIRNAME). A
# session folder is "<task_dir>/InProgress/<session_counter>", so the task's
# Logs folder sits two levels up, beside "InProgress".
TASK_LOGS_DIRNAME = "Logs"


def _logs_dir_for(task_identifier: str) -> str | None:
    """Derive a task's ``Logs`` folder from an InProgress session-folder path.

    Mirrors how ``Tasks/task_runner.py`` builds ``DiscoveredTask.logs_dir``
    (``<task_dir>/Logs``). ``task_identifier`` is the decoded
    ``<task_dir>/InProgress/<session_counter>`` path, so the task folder is two
    levels up. Returns None when ``task_identifier`` is not an absolute session
    path (e.g. a standalone label), so logging falls back to the global log.
    """
    if not task_identifier or not os.path.isabs(task_identifier):
        return None
    normalized = os.path.normpath(task_identifier)
    task_dir = os.path.dirname(os.path.dirname(normalized))
    return os.path.join(task_dir, TASK_LOGS_DIRNAME)


def _log(logs_dir, level: str, message: str) -> None:
    """Log against a task when ``logs_dir`` is set, else to the global log."""
    if logs_dir:
        getattr(logger, f"log_task_{level}")(logs_dir, SCRIPT_NAME, message)
    else:
        getattr(logger, f"log_{level}")(SCRIPT_NAME, message)


def _validate_argument(name: str, value) -> str:
    """Validate that a mandatory string argument is present and non-empty.

    Args:
        name: The argument name (used in the error message and logs).
        value: The value to validate.

    Returns:
        The validated value, stripped of surrounding whitespace.

    Raises:
        ValueError: If the value is missing, not a string, or blank.
    """
    if value is None or not isinstance(value, str) or not value.strip():
        message = f"'{name}' is required and must be a non-empty string."
        logger.log_error(SCRIPT_NAME, f"Validation failed: {message}")
        raise ValueError(message)
    return value.strip()


def extract_web_page(task_identifier: str, web_url: str, destination_folder_name: str | None = None) -> str | None:
    """Public entry point: capture a web page via the Lyrebird extension.

    Validates the mandatory arguments, assembles a web_extract task, and
    delegates to ``run_web_extract_task``.

    Args:
        task_identifier: A label identifying this extraction (used for logging
            and as the task name). Mandatory. When it is an absolute path to the
            current task's ``InProgress`` session folder, it also determines the
            default destination (see below).
        web_url: The URL of the page to capture. Mandatory.
        destination_folder_name: Optional. The folder name (or absolute path)
            the captured page is moved into. When omitted or blank, the capture
            is stored in a ``web_extract`` folder inside the current task folder
            (i.e. ``<task_identifier>/web_extract``).

    Returns:
        The full path of the captured file on success, or None on failure.

    Raises:
        ValueError: If ``task_identifier`` or ``web_url`` is missing or blank.
    """
    task_identifier = _validate_argument("task_identifier", task_identifier)
    web_url = _validate_argument("web_url", web_url)

    # destination_folder_name is optional; normalize a blank value to None so
    # run_web_extract_task can apply its default.
    if isinstance(destination_folder_name, str):
        destination_folder_name = destination_folder_name.strip() or None

    # When task_identifier is the decoded absolute InProgress session folder,
    # derive the owning task's Logs folder so this run's log lines (here and in
    # run_web_extract_task) are recorded against the task, matching task_runner.
    logs_dir = _logs_dir_for(task_identifier)

    _log(
        logs_dir,
        "info",
        f"extract_web_page called (task_identifier='{task_identifier}', "
        f"web_url='{web_url}', destination_folder_name='{destination_folder_name}').",
    )

    task = {
        "name": task_identifier,
        "web_url": web_url,
        "destination_folder_name": destination_folder_name,
    }

    return run_web_extract_task(task, logs_dir=logs_dir)


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for external command-line callers."""
    parser = argparse.ArgumentParser(
        description="Capture a web page via the Lyrebird browser extension."
    )
    parser.add_argument(
        "--task-identifier",
        required=True,
        help="A label identifying this extraction (used for logging and as the task name).",
    )
    parser.add_argument(
        "--web-url",
        required=True,
        help="The URL of the page to capture.",
    )
    parser.add_argument(
        "--destination-folder-name",
        required=True,
        help="The folder name (or absolute path) the captured page is moved into.",
    )
    return parser


def main() -> int:
    """CLI wrapper around ``extract_web_page``.

    Returns:
        Process exit code: 0 on success, 1 on failure.
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    try:
        result_path = extract_web_page(
            task_identifier=args.task_identifier,
            web_url=args.web_url,
            destination_folder_name=args.destination_folder_name,
        )
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    if result_path:
        print(f"Capture complete: {result_path}")
        return 0

    print("Capture failed. See the Kelpie log for details.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

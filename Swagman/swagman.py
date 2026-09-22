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


def extract_web_page(task_identifier: str, web_url: str, destination_folder_name: str) -> str | None:
    """Public entry point: capture a web page via the Lyrebird extension.

    Validates the three mandatory arguments, assembles a web_extract task, and
    delegates to ``run_web_extract_task``.

    Args:
        task_identifier: A label identifying this extraction (used for logging
            and as the task name). Mandatory.
        web_url: The URL of the page to capture. Mandatory.
        destination_folder_name: The folder name (or absolute path) the captured
            page is moved into. Mandatory.

    Returns:
        The full path of the captured file on success, or None on failure.

    Raises:
        ValueError: If any of the three arguments is missing or blank.
    """
    task_identifier = _validate_argument("task_identifier", task_identifier)
    web_url = _validate_argument("web_url", web_url)
    destination_folder_name = _validate_argument("destination_folder_name", destination_folder_name)

    logger.log_info(
        SCRIPT_NAME,
        f"extract_web_page called (task_identifier='{task_identifier}', "
        f"web_url='{web_url}', destination_folder_name='{destination_folder_name}').",
    )

    task = {
        "name": task_identifier,
        "web_url": web_url,
        "destination_folder_name": destination_folder_name,
    }

    return run_web_extract_task(task)


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

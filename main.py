"""
Kelpie Main Script - Executed periodically via Windows Task Scheduler.

Orchestrates the task runner on each scheduled invocation.
"""

import os
import sys

# Ensure the project root is on the path so all modules can be imported
# regardless of the working directory Task Scheduler uses.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Logger"))

import kelpie_logger as logger
from Tasks.task_runner import run_all_tasks

SCRIPT_NAME = "main.py"


def main():
    logger.log_info(SCRIPT_NAME, "Scheduled execution started.")

    try:
        run_all_tasks()
        logger.log_info(SCRIPT_NAME, "All tasks processed successfully.")
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Task runner failed: {e}")
        sys.exit(1)

    logger.log_info(SCRIPT_NAME, "Scheduled execution completed.")


if __name__ == "__main__":
    main()

"""
Kelpie Main Script - Executed periodically via Windows Task Scheduler.

Orchestrates the task runner on each scheduled invocation.

The scheduler triggers this script every minute. To prevent overlapping
runs, a single-instance lock is acquired at startup. If a previous run is
still executing, the current invocation logs a message and exits without
running the tasks.
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

# Lock file used to guarantee only one instance runs at a time.
LOCK_FILE = os.path.join(PROJECT_ROOT, "kelpie.lock")


def _pid_is_running(pid: int) -> bool:
    """Return True if a process with the given PID is currently running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # Query the Windows task list for the PID.
        import subprocess

        try:
            output = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:
            # If we cannot determine, assume it is running to stay safe.
            return True
        return str(pid) in output
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def acquire_lock() -> bool:
    """Attempt to acquire the single-instance lock.

    Returns:
        True if the lock was acquired, False if another instance is running.
    """
    try:
        # Atomically create the lock file; fails if it already exists.
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    except FileExistsError:
        # Lock exists. Check whether the owning process is still alive.
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            owner_pid = int(content) if content else -1
        except (OSError, ValueError):
            owner_pid = -1

        if _pid_is_running(owner_pid):
            # A previous run is still executing.
            return False

        # Stale lock left by a crashed run. Reclaim it.
        logger.log_warning(
            SCRIPT_NAME,
            f"Removing stale lock file from dead PID {owner_pid}.",
        )
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
        return acquire_lock()


def release_lock():
    """Remove the lock file if it belongs to this process."""
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            owner_pid = int(f.read().strip())
    except (OSError, ValueError):
        owner_pid = -1

    if owner_pid == os.getpid():
        try:
            os.remove(LOCK_FILE)
        except OSError as e:
            logger.log_error(SCRIPT_NAME, f"Failed to remove lock file: {e}")


def main():
    # Begin a new logging session: increments and persists the session
    # counter, which stays constant for all logging in this execution.
    logger.start_session()

    if not acquire_lock():
        logger.log_info(
            SCRIPT_NAME,
            "Previous run still in progress; skipping this execution.",
        )
        return

    logger.log_info(SCRIPT_NAME, "Scheduled execution started.")

    try:
        run_all_tasks()
        logger.log_info(SCRIPT_NAME, "All tasks processed successfully.")
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Task runner failed: {e}")
        sys.exit(1)
    finally:
        release_lock()

    logger.log_info(SCRIPT_NAME, "Scheduled execution completed.")
    logger.log_info(SCRIPT_NAME, "----------------------------------------------------------------------------------------------------")
    logger.log_info(SCRIPT_NAME, "")
    logger.log_info(SCRIPT_NAME, "")

if __name__ == "__main__":
    main()

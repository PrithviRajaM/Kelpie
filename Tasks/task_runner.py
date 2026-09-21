"""
Task Runner - Core scheduling logic for Kelpie.

Tasks are no longer defined in a single ``task_config.json`` with prompts in a
shared ``Templates`` folder. Instead, every task lives inside a user's profile
folder under the Numbat data root:

    <NUMBAT_DATA_ROOT>/
    └── <email>/                    # profile folder
        └── Tasks/
            └── <task_name>/        # folder name == task name
                ├── TaskConfig.json # persisted task config
                ├── TaskPrompt.txt  # prompt text fed to the model
                └── Logs/           # per-task logs, one file per date

    The data root comes from the global config (``kelpie_config.json``,
    key ``data_root``); default ``D:\\Data\\Numbat``, overridable per-run via
    the ``NUMBAT_DATA_ROOT`` environment variable.

On every run this module discovers all ``TaskConfig.json`` files nested under
the data root, retaining each task's folder location so that:
  - the prompt is read from that task's own ``TaskPrompt.txt`` at execution time;
  - logs for that task are written into that task's own ``Logs`` folder.

Last-execution timestamps are persisted in ``task_execution_state.json`` (next
to this module), keyed by ``<email>/<task_name>`` so tasks with the same name
in different profiles never collide.
"""

import json
import os
import sys
from datetime import datetime, timedelta

# Resolve paths relative to the project root (one level up from Tasks/)
TASKS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TASKS_DIR)

sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Logger"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Ollama"))

import kelpie_logger as logger
import config as kelpie_config
from Tasks.prompt_builders import build_prompt
from ollama_client import load_config as load_ollama_config, get_client, resolve_model, send_message

SCRIPT_NAME = "task_runner.py"

# Root of the profile-based task data comes from the centralized global config
# (kelpie_config.json). It is still overridable per-run via the
# NUMBAT_DATA_ROOT environment variable (handled inside config.get_data_root).
DATA_ROOT = kelpie_config.get_data_root()

# Per-profile tasks live under "<email>/Tasks/<task_name>".
PROFILE_TASKS_SUBDIR = "Tasks"

# Fixed filenames inside each task folder (see TASKS.md §1).
TASK_CONFIG_FILENAME = "TaskConfig.json"
TASK_PROMPT_FILENAME = "TaskPrompt.txt"
TASK_LOGS_DIRNAME = "Logs"

# Execution state stays local to the Kelpie install (not per-profile).
EXECUTION_STATE_PATH = os.path.join(TASKS_DIR, "task_execution_state.json")


class DiscoveredTask:
    """A task found on disk, with everything needed to run and log it.

    Attributes:
        email: The owning profile (folder name directly under the data root).
        name: The task name (== its folder name and its config 'name').
        config: The parsed TaskConfig.json contents.
        task_dir: Absolute path to the task's folder.
        prompt_path: Absolute path to the task's TaskPrompt.txt.
        logs_dir: Absolute path to the task's Logs folder.
        state_key: Stable key for execution state ("<email>/<name>").
    """

    def __init__(self, email: str, name: str, config: dict, task_dir: str):
        self.email = email
        self.name = name
        self.config = config
        self.task_dir = task_dir
        self.prompt_path = os.path.join(task_dir, TASK_PROMPT_FILENAME)
        self.logs_dir = os.path.join(task_dir, TASK_LOGS_DIRNAME)
        self.state_key = f"{email}/{name}"


def discover_tasks() -> list:
    """Discover every task under the data root, retaining its location.

    Walks ``<DATA_ROOT>/<email>/Tasks/<task_name>`` and treats any folder that
    contains a ``TaskConfig.json`` as a task. The config's parsed contents and
    the folder path are both retained so execution can later read the task's
    own prompt and write to the task's own Logs folder.

    Returns:
        A list of ``DiscoveredTask`` objects (may be empty).
    """
    if not os.path.isdir(DATA_ROOT):
        logger.log_error(SCRIPT_NAME, f"Data root does not exist: {DATA_ROOT}")
        return []

    discovered = []

    for email in sorted(os.listdir(DATA_ROOT)):
        profile_dir = os.path.join(DATA_ROOT, email)
        if not os.path.isdir(profile_dir):
            continue

        tasks_dir = os.path.join(profile_dir, PROFILE_TASKS_SUBDIR)
        if not os.path.isdir(tasks_dir):
            # A profile with no Tasks folder simply has no tasks yet.
            continue

        for task_name in sorted(os.listdir(tasks_dir)):
            task_dir = os.path.join(tasks_dir, task_name)
            if not os.path.isdir(task_dir):
                continue

            config_path = os.path.join(task_dir, TASK_CONFIG_FILENAME)
            if not os.path.isfile(config_path):
                # Only folders holding a TaskConfig.json are tasks.
                continue

            config = _load_task_config(config_path)
            if config is None:
                continue

            discovered.append(DiscoveredTask(email, task_name, config, task_dir))

    logger.log_info(
        SCRIPT_NAME,
        f"Discovered {len(discovered)} task(s) under {DATA_ROOT}.",
    )
    return discovered


def _load_task_config(config_path: str) -> dict | None:
    """Load and parse a single TaskConfig.json. Returns None on failure."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.log_error(SCRIPT_NAME, f"Task config not found: {config_path}")
        return None
    except json.JSONDecodeError as e:
        logger.log_error(SCRIPT_NAME, f"Task config is invalid JSON ({config_path}): {e}")
        return None

    if not isinstance(data, dict):
        logger.log_error(SCRIPT_NAME, f"Task config has unexpected structure: {config_path}")
        return None

    return data


def load_execution_state() -> dict:
    """Load the execution state file that tracks last run times."""
    if not os.path.exists(EXECUTION_STATE_PATH):
        return {"tasks": {}}

    try:
        with open(EXECUTION_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.log_warning(SCRIPT_NAME, f"Could not read execution state, resetting: {e}")
        return {"tasks": {}}


def save_execution_state(state: dict) -> None:
    """Persist the execution state back to disk."""
    try:
        with open(EXECUTION_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
    except IOError as e:
        logger.log_error(SCRIPT_NAME, f"Failed to save execution state: {e}")


def is_task_due(task: "DiscoveredTask", frequency_minutes: int, state: dict) -> bool:
    """Determine whether a task is due for execution based on its frequency.

    Returns True if the task has never run or if enough time has elapsed.
    Progress is logged both globally and into the task's own Logs folder.
    """
    task_state = state.get("tasks", {}).get(task.state_key)

    if not task_state or not task_state.get("last_execution"):
        msg = f"Task '{task.name}' has never been executed. It is due."
        logger.log_info(SCRIPT_NAME, msg)
        logger.log_task_info(task.logs_dir, SCRIPT_NAME, msg)
        return True

    last_run_str = task_state["last_execution"]
    try:
        last_run = datetime.fromisoformat(last_run_str)
    except ValueError:
        msg = f"Invalid last_execution timestamp for '{task.name}'. Treating as due."
        logger.log_warning(SCRIPT_NAME, msg)
        logger.log_task_warning(task.logs_dir, SCRIPT_NAME, msg)
        return True

    next_due = last_run + timedelta(minutes=frequency_minutes)
    now = datetime.now()

    if now >= next_due:
        elapsed = (now - last_run).total_seconds() / 60
        msg = f"Task '{task.name}' is due. Last ran {elapsed:.1f} min ago (frequency: {frequency_minutes} min)."
        logger.log_info(SCRIPT_NAME, msg)
        logger.log_task_info(task.logs_dir, SCRIPT_NAME, msg)
        return True

    remaining = (next_due - now).total_seconds() / 60
    logger.log_info(SCRIPT_NAME, f"Task '{task.name}' is not due yet. Next run in {remaining:.1f} min.")
    return False


def load_prompt(task: "DiscoveredTask") -> str | None:
    """Load the task's prompt from its own TaskPrompt.txt.

    The prompt is stored verbatim; an empty prompt is allowed and returned as
    an empty string. Only a missing/unreadable file yields None.
    """
    if not os.path.isfile(task.prompt_path):
        msg = f"Prompt not found for task '{task.name}': {task.prompt_path}"
        logger.log_error(SCRIPT_NAME, msg)
        logger.log_task_error(task.logs_dir, SCRIPT_NAME, msg)
        return None

    try:
        with open(task.prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except IOError as e:
        msg = f"Failed to read prompt for '{task.name}': {e}"
        logger.log_error(SCRIPT_NAME, msg)
        logger.log_task_error(task.logs_dir, SCRIPT_NAME, msg)
        return None


def execute_task(task: "DiscoveredTask", prompt_content: str) -> str | None:
    """Execute a task: build prompt, call Ollama, return the response.

    All progress is mirrored into the task's own Logs folder so each task's
    run history lives alongside its config and prompt.
    """
    logs_dir = task.logs_dir

    # Build prompt using the extensible prompt builder
    prompt = build_prompt(task.name, prompt_content)
    logger.log_info(SCRIPT_NAME, f"Prompt built for task '{task.name}': {prompt[:100]}...")
    logger.log_task_info(logs_dir, SCRIPT_NAME, f"Prompt built for task '{task.name}'.")

    # Load Ollama config
    try:
        ollama_config = load_ollama_config()
    except Exception as e:
        msg = f"Failed to load Ollama config for task '{task.name}': {e}"
        logger.log_error(SCRIPT_NAME, msg)
        logger.log_task_error(logs_dir, SCRIPT_NAME, msg)
        return None

    try:
        client = get_client(ollama_config)
        model = resolve_model(None, ollama_config)
        keep_alive = ollama_config.get("keep_alive", "5m")
    except Exception as e:
        msg = f"Failed to initialize Ollama client for task '{task.name}': {e}"
        logger.log_error(SCRIPT_NAME, msg)
        logger.log_task_error(logs_dir, SCRIPT_NAME, msg)
        return None

    logger.log_info(SCRIPT_NAME, f"Task '{task.name}' calling Ollama model '{model}'...")
    logger.log_task_info(logs_dir, SCRIPT_NAME, f"Calling Ollama model '{model}'.")
    response = send_message(client, model, prompt, keep_alive)

    if response:
        logger.log_info(SCRIPT_NAME, f"Task '{task.name}' completed. Response length: {len(response)} chars.")
        logger.log_info(SCRIPT_NAME, f"Task '{task.name}' response: {response[:500]}")
        logger.log_task_info(logs_dir, SCRIPT_NAME, f"Completed. Response length: {len(response)} chars.")
    else:
        msg = f"Task '{task.name}' received no response from Ollama."
        logger.log_error(SCRIPT_NAME, msg)
        logger.log_task_error(logs_dir, SCRIPT_NAME, msg)

    return response


def run_all_tasks() -> None:
    """Main entry point: discover, evaluate, and run all tasks on disk."""
    logger.log_info(SCRIPT_NAME, "Task runner started.")

    tasks = discover_tasks()
    if not tasks:
        logger.log_warning(SCRIPT_NAME, f"No tasks found under {DATA_ROOT}.")
        return

    state = load_execution_state()

    for task in tasks:
        # The config's own 'name' is preferred; fall back to the folder name.
        config_name = task.config.get("name") or task.name

        # Check if task is enabled
        enabled = task.config.get("enabled", False)
        if not enabled:
            msg = f"Task '{config_name}' is disabled. Skipping."
            logger.log_info(SCRIPT_NAME, msg)
            logger.log_task_info(task.logs_dir, SCRIPT_NAME, msg)
            continue

        # Parse frequency
        try:
            frequency_minutes = int(task.config.get("frequency_in_minutes", 0))
        except (ValueError, TypeError):
            msg = f"Task '{config_name}' has invalid frequency_in_minutes. Skipping."
            logger.log_error(SCRIPT_NAME, msg)
            logger.log_task_error(task.logs_dir, SCRIPT_NAME, msg)
            continue

        if frequency_minutes <= 0:
            msg = f"Task '{config_name}' has frequency <= 0. Skipping."
            logger.log_error(SCRIPT_NAME, msg)
            logger.log_task_error(task.logs_dir, SCRIPT_NAME, msg)
            continue

        # Check if task is due
        if not is_task_due(task, frequency_minutes, state):
            continue

        # Task is qualified to run
        logger.log_info(SCRIPT_NAME, f"Task '{config_name}' ({task.email}) has started to run.")
        logger.log_task_info(task.logs_dir, SCRIPT_NAME, f"Task '{config_name}' has started to run.")

        # Load the task's own prompt
        prompt_content = load_prompt(task)
        if prompt_content is None:
            continue

        # Execute the task
        response = execute_task(task, prompt_content)

        # Save execution timestamp regardless of success (to avoid retry storms)
        if "tasks" not in state:
            state["tasks"] = {}

        state["tasks"][task.state_key] = {
            "email": task.email,
            "name": config_name,
            "last_execution": datetime.now().isoformat(),
            "last_status": "success" if response else "failed",
        }
        save_execution_state(state)

    logger.log_info(SCRIPT_NAME, "Task runner completed.")


if __name__ == "__main__":
    run_all_tasks()
